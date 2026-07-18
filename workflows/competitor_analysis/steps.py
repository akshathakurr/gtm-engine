"""Per-competitor enrichment steps for the competitor analysis workflow.

Each function is an independent lookup (website scrape, firmographics, founders,
reviews, final Claude analysis, …). They take a company name/URL/scraped data
and return raw values; orchestration and sheet/CSV I/O live in ``workflow.py``.
"""

import json
import re
from typing import List, Dict
from urllib.parse import urlparse

import anthropic

from config import CLAUDE_MODEL, cached_system
from workflows._common import (
    strip_json_fence as _strip_json_fence,
    map_rate_limited,
)
from scrapers.web_search.scraper import search_web
from scrapers.website_scraper import scraper as _website_mod
from scrapers.linkedin_profile_post_scraper import scraper as _li_posts_mod
from scrapers.twitter_profile_scraper import scraper as _twitter_mod
from scrapers.review_scraper import scraper as _review_mod
from scrapers.firecrawl_scraper import scraper as _firecrawl_mod



# ---------------------------------------------------------------------------
# Per-competitor concurrency + small local helpers
# (shared sheet/context/JSON helpers live in workflows/_common.py)
# ---------------------------------------------------------------------------

# Each competitor's independent enrichment lookups (Claude + Exa) run together
# in a bounded pool. Web search is globally throttled + thread-safe, so many of
# these in flight at once is safe.
COMPETITOR_ENRICH_CONCURRENCY = 6

# Companies are processed concurrently across a bounded pool. Kept modest: each
# company already fans its own enrichment across COMPETITOR_ENRICH_CONCURRENCY
# threads, so total in-flight work is COMPANY_CONCURRENCY x that. Backend writes
# stay on the main thread (see workflow.py), so the sheet/CSV is never touched
# concurrently.
COMPANY_CONCURRENCY = 3

# The founder-post and review scrapers hit throttled Apify actors. With companies
# running concurrently, those calls would otherwise burst; a single shared
# limiter spaces every actor call start >= this many seconds apart, process-wide
# (replaces the old per-company time.sleep(2) between founders).
APIFY_MIN_INTERVAL = 2.0


# Exact Bird Eye output header names (Notes is manual, excluded). One source of
# truth — workflow.py imports this for header setup, process_competitor for the
# final-analysis profile.
OUTPUT_COLUMNS = [
    "Company LinkedIn URL",
    "Company Description",
    "Competitor Score",
    "Strength",
    "Weakness",
    "Employee Count",
    "Founded Year",
    "Last Funding Stage",
    "Total Funding",
    "Est. Revenue",
    "HQ Location",
    "Recent News",
    "Founder (1) Name",
    "Founder (1) LinkedIn",
    "Founder (1) Twitter",
    "Founder (1) Post type",
    "Founder (2) Name",
    "Founder (2) LinkedIn",
    "Founder (2) Twitter",
    "Founder (2) Post type",
    "Target Persona (User)",
    "Primary CTA",
    "Pricing",
    "Customer Stories",
    "Product Features",
    "Customer Reviews",
    "Target ICP",
    "Sales Motion",
    "Deal Size",
    "Top Logos",
    "Marketing Messaging",
    "Content Type",
    "SEO",
]


def _run_parallel(tasks: Dict[str, "callable"], max_workers: int = COMPETITOR_ENRICH_CONCURRENCY) -> Dict[str, tuple]:
    """Run named zero-arg thunks concurrently. Returns {key: (result, error)}.

    Exceptions are captured per task (never raised), so one failed lookup can't
    abort the others or the competitor. Thin wrapper over _common.map_rate_limited.
    """
    keys = list(tasks)
    results, errors = map_rate_limited(
        lambda fn: fn(), [tasks[k] for k in keys], max_workers=max_workers,
    )
    return dict(zip(keys, zip(results, errors)))


def _strip_www(netloc: str) -> str:
    """Drop a leading 'www.' (str.lstrip('www.') would strip stray w/./3 chars)."""
    return netloc[4:] if netloc.startswith("www.") else netloc


def _cap(snippets: List[str], limit: int) -> List[str]:
    """Trim each snippet to ~500 chars (keeps Claude input small) then cap count."""
    return [(s or "")[:500] for s in snippets[:limit]]


# ---------------------------------------------------------------------------
# Step 1: Website scrape
# ---------------------------------------------------------------------------

def scrape_website(url: str) -> dict:
    """Scrape a company website. Uses Firecrawl (JS-rendered, reliable) when
    FIRECRAWL_API_KEY is set; falls back to the static scraper otherwise."""
    if not url:
        return {}
    try:
        fc = _firecrawl_mod.scrape_website(url)
        # Firecrawl returns None with no key, and an empty result (no pages) on
        # fetch/quota failure — fall back to the static scraper in both cases.
        if fc is not None and fc.get("full_text_by_page"):
            return fc
        return _website_mod.scrape_website(url=url)
    except Exception as e:
        print(f"    Website scrape error: {e}")
        try:
            return _website_mod.scrape_website(url=url)
        except Exception:
            return {}


# Generic slugs are common false positives from the co-mention search — never a
# competitor's real page.
_JUNK_LI_SLUGS = {"linkedin", "company", "companies", "school"}


def _is_real_company_li(url: str) -> bool:
    slug = url.split("/company/")[-1].strip("/").lower()
    return bool(slug) and slug not in _JUNK_LI_SLUGS


# Social / aggregator domains that are never a company's own homepage — used to
# reject noise when searching for an official site from just a name.
_NON_OFFICIAL_DOMAINS = (
    "linkedin.com", "twitter.com", "x.com", "facebook.com", "instagram.com",
    "youtube.com", "tiktok.com", "crunchbase.com", "wikipedia.org", "g2.com",
    "capterra.com", "glassdoor.com", "bloomberg.com", "medium.com",
    "github.com", "reddit.com", "pitchbook.com", "apollo.io",
)


def find_official_site(company_name: str, client: anthropic.Anthropic) -> str:
    """Best-effort official homepage for a company given only its name — used when
    the input row has no URL, so the website scrape and every website-derived step
    (description, product info, CTA…) have something to work with. Returns '' if
    nothing confident turns up."""
    try:
        result = search_web(
            query=f"{company_name} official website",
            num_results=6,
            summary_question=f"What is the official company homepage URL for {company_name}?",
            include_summary=False,  # we only read result URLs here
        )
    except Exception as e:
        print(f"    Official-site lookup failed: {e}")
        return ""

    seen, unique = set(), []
    for r in result.get("results", []):
        parsed = urlparse((r.get("url", "") or "").split("?")[0])
        domain = _strip_www(parsed.netloc).lower()
        if not domain or any(domain == d or domain.endswith("." + d)
                             for d in _NON_OFFICIAL_DOMAINS):
            continue
        home = f"{parsed.scheme or 'https'}://{parsed.netloc}"
        if home not in seen:
            seen.add(home); unique.append(home)

    if not unique:
        return ""
    if len(unique) == 1:
        return unique[0]
    # Multiple candidates — let Claude pick the one that's actually this company.
    try:
        resp = client.messages.create(
            model=CLAUDE_MODEL, max_tokens=100,
            messages=[{"role": "user", "content": (
                f"Which of these is the official homepage of the company {company_name!r}?\n"
                f"Options: {unique}\n"
                f'Return JSON: {{"url": "https://..."}} — or {{"url": ""}} if none clearly match.\n'
                f"Return only valid JSON."
            )}],
        )
        chosen = json.loads(_strip_json_fence(resp.content[0].text)).get("url", "")
        if chosen == "":
            return ""  # Claude says none of the candidates is this company
        return chosen if chosen in unique else unique[0]
    except Exception:
        return unique[0]


# ---------------------------------------------------------------------------
# Step 2: Company LinkedIn URL
# ---------------------------------------------------------------------------

def find_linkedin_url(company_name: str, website: str,
                      client: anthropic.Anthropic) -> str:
    """Extract LinkedIn company URL by fetching the homepage via Jina Reader
    (which includes href link URLs in its markdown output). Falls back to web search."""
    import requests as _requests

    pattern = r'https?://(?:[\w-]+\.)?linkedin\.com/company/([A-Za-z0-9_-]+)'
    domain  = _strip_www(urlparse(website).netloc) if website else ""
    dp      = re.sub(r"[^a-z0-9]", "", domain.split(".")[0].lower()) if domain else ""

    # --- Pass 1: Jina Reader on homepage (includes href attributes) ---
    if website:
        try:
            jina_url = f"https://r.jina.ai/{website.rstrip('/')}"
            resp = _requests.get(jina_url,
                                 headers={"User-Agent": "Mozilla/5.0", "Accept": "text/plain"},
                                 timeout=20)
            if resp.ok:
                jina_text = resp.text
                slugs = re.findall(pattern, jina_text)
                # Remove duplicates + generic junk slugs, keep order
                seen, unique = set(), []
                for s in slugs:
                    if s not in seen and s.lower() not in _JUNK_LI_SLUGS:
                        seen.add(s); unique.append(s)
                if unique:
                    # Prefer slug that matches domain prefix
                    if dp:
                        for s in unique:
                            if dp in re.sub(r"[^a-z0-9]", "", s.lower()):
                                return f"https://www.linkedin.com/company/{s}"
                    return f"https://www.linkedin.com/company/{unique[0]}"
        except Exception:
            pass

    # --- Pass 2: web search + Claude disambiguation ---
    # Co-mention query: forces domain + linkedin.com/company to appear together.
    # With no website/domain, anchor on the quoted company name instead.
    anchor = domain or f'"{company_name}"'
    try:
        result = search_web(
            query=f'{anchor} linkedin.com/company',
            num_results=3,
            summary_question=f"What is the LinkedIn company page URL for {company_name} ({domain})?",
            include_summary=False,  # we only read result URLs here
        )
        li_urls = [
            u for u in (
                r.get("url", "").split("?")[0].rstrip("/")
                for r in result.get("results", [])
                if "linkedin.com/company/" in r.get("url", "")
            )
            if _is_real_company_li(u)
        ]
        if not li_urls:
            return ""
        if len(li_urls) == 1:
            return li_urls[0]
        # Multiple results — let Claude pick the right one using domain + name context
        try:
            resp = client.messages.create(
                model=CLAUDE_MODEL, max_tokens=100,
                messages=[{"role": "user", "content": (
                    f"Which LinkedIn company URL belongs to {company_name} whose website is {website}?\n"
                    f"Options: {li_urls}\n"
                    f'Return JSON: {{"url": "https://..."}}\nReturn only valid JSON.'
                )}],
            )
            chosen = json.loads(_strip_json_fence(resp.content[0].text)).get("url", "")
            return chosen if chosen in li_urls else li_urls[0]
        except Exception:
            # Fallback: prefer slug containing domain prefix
            for url in li_urls:
                if dp and dp in re.sub(r"[^a-z0-9]", "", url.split("/company/")[-1].lower()):
                    return url
            return li_urls[0]
    except Exception as e:
        print(f"    LinkedIn URL search failed: {e}")
    return ""


# ---------------------------------------------------------------------------
# Step 3: Company description
# ---------------------------------------------------------------------------

def draft_description(company_name: str, scraped: dict, client: anthropic.Anthropic) -> str:
    source = (
        scraped.get("meta_description")
        or scraped.get("product_description")
        or scraped.get("homepage_text", "")[:600]
    )
    if not source:
        return ""
    try:
        resp = client.messages.create(
            model=CLAUDE_MODEL, max_tokens=150,
            messages=[{"role": "user", "content": (
                f"Write a single clear one-liner (max 2 sentences) describing what {company_name} does"
                f" based on this text. Be specific about the product and who it serves.\n\n"
                f"Text: {source[:800]}\n\n"
                f'Return JSON: {{"description": "..."}}\nReturn only valid JSON.'
            )}],
        )
        return json.loads(_strip_json_fence(resp.content[0].text)).get("description", "")
    except Exception as e:
        print(f"    Description failed: {e}")
        return ""


# ---------------------------------------------------------------------------
# Step 4: Firmographics
# ---------------------------------------------------------------------------

def get_firmographics(company_name: str, website: str,
                      client: anthropic.Anthropic) -> Dict[str, str]:
    empty = {
        "Employee Count": "", "Founded Year": "", "Last Funding Stage": "",
        "Total Funding": "", "Est. Revenue": "", "HQ Location": "",
    }
    # Anchor to the domain when we have one so a same-named company doesn't thin
    # the firmographics (e.g. "Bluebird" the airline vs. the SaaS).
    domain = _strip_www(urlparse(website).netloc) if website else ""
    label = f"{company_name} ({domain})" if domain else company_name
    query = (
        f'"{domain}" {company_name} employees headcount funding stage total funding founded year headquarters revenue'
        if domain else
        f"{company_name} employees headcount funding stage total funding founded year headquarters revenue"
    )
    try:
        result = search_web(
            query=query,
            num_results=3,
            summary_question=(
                f"What is {label}'s employee count, year founded, last funding stage, "
                f"total funding raised, estimated revenue, and HQ city?"
            ),
        )
    except Exception as e:
        print(f"    Firmographics search failed: {e}")
        return empty

    snippets = []
    for r in result.get("results", []):
        if r.get("summary"):
            snippets.append(r["summary"])
        snippets.extend(r.get("highlights", []))
    if not snippets:
        return empty

    prompt = f"""Extract firmographic data for "{label}" from this research.
Only use facts about the company at {domain or company_name} — ignore any results about a different company that happens to share the name.

Research:
{chr(10).join(_cap(snippets, 6))}

Return JSON with exactly these keys:
- "Employee Count": exact number, no range (e.g. "120", "350"). If only a range, use midpoint.
- "Founded Year": 4-digit year only (e.g. "2019")
- "Last Funding Stage": MUST be one of these exact values (case-sensitive): "Pre-Seed", "Seed", "Series A", "Series B", "Series C", "Series D", "Series E+", "IPO", "Acquired", "Bootstrapped". If you only find a convertible note / SAFE / pre-priced round and no later priced round, use "Seed" (or "Pre-Seed" if explicitly described as pre-seed). Never invent new categories. If unknown, use "".
- "Total Funding": amount + unit (e.g. "10m", "50m", "500k", "1.2b"). m=millions, k=thousands, b=billions.
- "Est. Revenue": same format as Total Funding, or "not available"
- "HQ Location": city name only (e.g. "San Francisco", "New York", "London")

Return empty string for any field not found.

CRITICAL: Respond with ONLY the raw JSON object — start with {{ and end with }}. No preamble, no reasoning, no markdown fences."""

    # One retry: the model occasionally returns an empty completion, which
    # would silently drop all firmographics. Retry once before degrading.
    last_err = None
    for attempt in range(2):
        try:
            resp = client.messages.create(
                model=CLAUDE_MODEL, max_tokens=500,
                messages=[{"role": "user", "content": prompt}],
            )
            text = _strip_json_fence(resp.content[0].text) if resp.content else ""
            if not text:
                raise ValueError("empty completion")
            return json.loads(text)
        except Exception as e:
            last_err = e
    print(f"    Firmographics extraction failed after retry: {last_err}")
    return empty


# ---------------------------------------------------------------------------
# Step 5: Recent news
# ---------------------------------------------------------------------------

def get_recent_news(company_name: str, website: str, client: anthropic.Anthropic) -> str:
    domain = _strip_www(urlparse(website).netloc) if website else ""
    # Anchor search to the domain to avoid generic name collisions
    query = (
        f'"{domain}" funding launch announcement event customer 2024 2025'
        if domain else
        f'"{company_name}" funding launch announcement event customer 2024 2025'
    )
    try:
        result = search_web(
            query=query,
            num_results=3,
            summary_question=f"What are the most recent notable news items about {company_name} ({domain})? Events, funding, customers, launches.",
        )
    except Exception as e:
        print(f"    News search failed: {e}")
        return ""

    candidates = []
    for r in result.get("results", []):
        # .get(key, "") still yields None when the key exists with a null value
        url   = r.get("url") or ""
        title = r.get("title") or ""
        snip  = r.get("snippet") or r.get("summary") or ""
        if url and title:
            candidates.append({"title": title, "url": url, "snippet": snip[:200]})
    if not candidates:
        return ""

    block = "\n".join(
        f"{i+1}. {c['title']} — {c['url']}\n   {c['snippet']}"
        for i, c in enumerate(candidates)
    )
    try:
        resp = client.messages.create(
            model=CLAUDE_MODEL, max_tokens=400,
            messages=[{"role": "user", "content": f"""Pick 2-3 recent news items that are SPECIFICALLY about the company "{company_name}" (website: {domain or "unknown"}).

Rules:
- Discard results about different companies that share the same name
- Discard generic directory listings (YC directory, Crunchbase profiles, LinkedIn profiles, startup databases) — these are not news
- Only keep actual news articles, press releases, or blog posts about a real event
- Focus on: funding rounds, hosted events, new customers, product launches, partnerships

Candidates:
{block}

Format each as: "One-liner about the news — [source URL]"
Example: "Raised $30m Series B — https://techcrunch.com/..."

If none qualify as actual news (only directories found), return an empty array.

Return JSON: {{"news": ["item1", "item2"]}}
Return only valid JSON."""}],
        )
        items = json.loads(_strip_json_fence(resp.content[0].text)).get("news", [])
        return "\n".join(items)
    except Exception as e:
        print(f"    News extraction failed for {company_name}: {e}")
        return ""


# ---------------------------------------------------------------------------
# Step 6: Founders
# ---------------------------------------------------------------------------

def _find_linkedin_in_url(founder_name: str, company_name: str) -> str:
    """Co-mention query to reliably surface a founder's linkedin.com/in/ URL."""
    try:
        result = search_web(
            query=f'"{founder_name}" "{company_name}" linkedin.com/in',
            num_results=3,
            summary_question=f"What is the LinkedIn profile URL for {founder_name} from {company_name}?",
        )
        # First look for a linkedin.com/in/ URL directly in result URLs
        for r in result.get("results", []):
            url = r.get("url", "")
            if "linkedin.com/in/" in url:
                return url.split("?")[0].rstrip("/")
        # Fall back to scanning all text (snippet, highlights, summary)
        for r in result.get("results", []):
            all_text = " ".join(filter(None, [
                r.get("snippet") or "", r.get("summary") or "",
                *r.get("highlights", []),
            ]))
            matches = re.findall(r'linkedin\.com/in/([A-Za-z0-9_-]+)', all_text)
            if matches:
                return f"https://www.linkedin.com/in/{matches[0]}"
    except Exception:
        pass
    return ""


def find_founders(company_name: str, website: str, client: anthropic.Anthropic) -> List[Dict[str, str]]:
    """Return up to 2 founders with name, linkedin, twitter."""
    domain = _strip_www(urlparse(website).netloc) if website else ""
    name_query = f'"{company_name}" {domain}' if domain else company_name

    # ── Step 1: Identify founder names ───────────────────────────────────────
    try:
        result = search_web(
            query=f"{name_query} founder CEO co-founder",
            num_results=3,
            summary_question=f"Who are the founders of {company_name} ({domain})? List their full names.",
        )
    except Exception as e:
        print(f"    Founder search failed: {e}")
        return []

    tw_urls: List[str] = []
    for r in result.get("results", []):
        for text in [r.get("snippet",""), r.get("summary",""), *r.get("highlights",[])]:
            for handle in re.findall(r'@([A-Za-z0-9_]{2,50})', text or ""):
                tw_urls.append(f"https://twitter.com/{handle}")
            for handle in re.findall(r'(?:twitter|x)\.com/([A-Za-z0-9_]{2,50})', text or ""):
                if handle.lower() not in ("search", "hashtag", "i", "intent", "share"):
                    tw_urls.append(f"https://twitter.com/{handle}")
        url = r.get("url", "")
        if "twitter.com/" in url or "x.com/" in url:
            clean = url.split("?")[0].rstrip("/").replace("x.com/", "twitter.com/")
            tw_urls.append(clean)

    snippets = []
    for r in result.get("results", []):
        if r.get("summary"):
            snippets.append(r["summary"])
        snippets.extend(r.get("highlights", []))
    if not snippets:
        return []

    try:
        resp = client.messages.create(
            model=CLAUDE_MODEL, max_tokens=300,
            messages=[{"role": "user", "content": f"""Extract up to 2 founder names of {company_name} ({domain}).

Research:
{chr(10).join(_cap(snippets, 6))}

Twitter URLs / handles found: {json.dumps(list(dict.fromkeys(tw_urls))[:10])}

Return JSON array of up to 2 founders. Leave linkedin and twitter empty — they will be looked up separately.
[
  {{"name": "Full Name", "linkedin": "", "twitter": "https://twitter.com/handle_if_found"}},
  ...
]
Return only valid JSON."""}],
        )
        founders = json.loads(_strip_json_fence(resp.content[0].text))[:2]
    except Exception as e:
        print(f"    Founder extraction failed: {e}")
        return []

    # ── Step 2: Co-mention search for each founder's LinkedIn /in/ URL ────────
    for f in founders:
        if f.get("name") and not f.get("linkedin"):
            print(f"    Finding LinkedIn for {f['name']}...")
            li_url = _find_linkedin_in_url(f["name"], company_name)
            if li_url:
                f["linkedin"] = li_url
                print(f"      → {li_url}")
            else:
                print("      → (not found)")

    # ── Step 3: Twitter — dedicated search for any founder still missing it ───
    for f in founders:
        if f.get("twitter"):
            f["twitter"] = f["twitter"].replace("x.com/", "twitter.com/")
        if f.get("name") and not f.get("twitter"):
            try:
                tw_res = search_web(
                    query=f'"{f["name"]}" {company_name} Twitter',
                    num_results=3,
                    summary_question=f"What is the Twitter/X handle of {f['name']} from {company_name}?",
                )
                for tr in tw_res.get("results", []):
                    all_texts = [
                        tr.get("url", ""),
                        tr.get("title", ""),
                        tr.get("snippet", "") or "",
                        tr.get("summary", "") or "",
                        *tr.get("highlights", []),
                    ]
                    for text in all_texts:
                        for turl in re.findall(r'https?://(?:twitter|x)\.com/([A-Za-z0-9_]{2,50})', text or ""):
                            if turl.lower() not in ("search", "hashtag", "i", "intent", "share"):
                                f["twitter"] = f"https://twitter.com/{turl}"
                                break
                        if f.get("twitter"):
                            break
                        name_parts = f["name"].lower().split()
                        for h in re.findall(r'@([A-Za-z0-9_]{2,50})', text or ""):
                            if any(part[:4] in h.lower() for part in name_parts):
                                f["twitter"] = f"https://twitter.com/{h}"
                                break
                        if f.get("twitter"):
                            break
                    if f.get("twitter"):
                        break
            except Exception:
                pass

    return founders


# ---------------------------------------------------------------------------
# Step 7: Founder post types
# ---------------------------------------------------------------------------

def get_founder_post_type(
    founder_name: str,
    linkedin_url: str,
    twitter_url: str,
    client: anthropic.Anthropic,
    skip_twitter: bool = False,
) -> str:
    """Fetch founder's recent posts and summarise content style in 1-2 sentences."""
    posts: List[str] = []

    if linkedin_url:
        try:
            res = _li_posts_mod.scrape_linkedin_profile_posts(
                profile_url=linkedin_url,
                max_posts=10,
                days_back=90,
            )
            for p in res.get("posts", []):
                txt = p.get("text", "").strip()
                if txt:
                    posts.append(f"[LinkedIn] {txt[:200]}")
        except Exception as e:
            print(f"    LinkedIn posts failed for {founder_name}: {e}")

    if twitter_url and not skip_twitter:
        try:
            res = _twitter_mod.scrape_twitter_profile(
                profile_url=twitter_url,
                max_tweets=15,
                days_back=90,
                include_retweets=False,
            )
            for t in res.get("tweets", []):
                txt = t.get("text", "").strip()
                if txt:
                    posts.append(f"[Twitter] {txt[:200]}")
        except Exception as e:
            print(f"    Twitter failed for {founder_name}: {e}")

    if not posts:
        return ""

    try:
        resp = client.messages.create(
            model=CLAUDE_MODEL, max_tokens=200,
            messages=[{"role": "user", "content": f"""Describe {founder_name}'s content strategy in 1-2 sentences based on these posts.

Posts:
{"---".join(posts[:12])}

Cover: what topics they post about (company metrics, industry insights, product updates, customer stories, funding, thought leadership, ICP engagement). Is this active founder-led content?

Return JSON: {{"post_type": "1-2 sentence summary"}}
Return only valid JSON."""}],
        )
        return json.loads(_strip_json_fence(resp.content[0].text)).get("post_type", "")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Step 8: Product info from website scrape
# ---------------------------------------------------------------------------

def extract_product_info(
    company_name: str,
    scraped: dict,
    client: anthropic.Anthropic,
) -> Dict[str, str]:
    """Extract all product + GTM fields from the already-scraped website data."""
    empty: Dict[str, str] = {
        "Target Persona (User)": "", "Sales Motion": "", "Primary CTA": "",
        "Pricing": "", "Customer Stories": "", "Product Features": "",
        "Top Logos": "", "Marketing Messaging": "", "SEO": "",
    }
    if not scraped:
        return empty

    page_texts = scraped.get("full_text_by_page", {})
    homepage   = scraped.get("homepage_text", "")[:2000]
    product_t  = pricing_t = customers_t = blog_t = ""

    # A site can expose several pages that match the same bucket (e.g. /pricing
    # AND /pages/pricing). Keep the one with the MOST content — a thin/empty
    # duplicate must not clobber the real page. Pricing tables also sit below
    # hero/FAQ copy, so keep enough of the page that price figures survive
    # (Popl's start at ~2135 chars).
    for key, text in page_texts.items():
        kl = key.lower()
        if any(kw in kl for kw in ["product", "platform", "feature", "solution"]):
            if len(text) > len(product_t):
                product_t = text[:2500]
        elif any(kw in kl for kw in ["pricing", "plan"]):
            if len(text) > len(pricing_t):
                pricing_t = text[:3000]
        elif any(kw in kl for kw in ["customer", "case", "stor", "client", "logo"]):
            if len(text) > len(customers_t):
                customers_t = text[:1500]
        elif any(kw in kl for kw in ["blog", "resource", "insight", "article"]):
            if len(text) > len(blog_t):
                blog_t = text[:2000]

    # Top logos: prefer scraper's extracted list, fall back to customers page text
    top_logos = ", ".join(scraped.get("customers", [])[:5])
    if not top_logos and customers_t:
        # Ask Claude to extract company names from the customers page text
        try:
            _r = client.messages.create(
                model=CLAUDE_MODEL, max_tokens=150,
                messages=[{"role": "user", "content": (
                    f"List up to 5 company names (not individual people) mentioned as customers on this page.\n\n"
                    f"Text:\n{customers_t[:1500]}\n\n"
                    f'Return JSON: {{"logos": ["Company A", "Company B"]}}\n'
                    f"Return only valid JSON. If no company names found, return empty array."
                )}],
            )
            logos = json.loads(_strip_json_fence(_r.content[0].text)).get("logos", [])
            top_logos = ", ".join(logos[:5])
        except Exception:
            pass

    context = f"""Homepage:
{homepage}

Product/Platform page:
{product_t}

Pricing page:
{pricing_t}

Customer stories/logos page:
{customers_t}"""

    prompt = f"""Analyze this website data for {company_name} and extract the following.

Website content:
{context}

Return JSON with these keys:
- "Target Persona (User)": end-user role/title (not company type). E.g. "AEs and BDRs", "Sales VPs", "SDRs", "Marketing Managers", "Founders"
- "Sales Motion": "PLG" if direct signup / free trial / start-for-free CTA is visible. "SLG" if the primary path is booking a demo or contacting sales.
- "Primary CTA": exact text of the main homepage CTA button (e.g. "Book a Demo", "Sign Up Free", "Get Started")
- "Pricing": plan names + prices only, no feature details (e.g. "Starter $49/mo, Pro $99/mo, Enterprise custom"). Write "not listed" if no pricing shown.
- "Customer Stories": if there are dedicated case study/story pages, write "Title — URL" for 1-2 of them. If only inline testimonials/quotes (no separate pages), write "Testimonials from [Company1], [Company2]". If nothing, write "".
- "Product Features": top 4 features as short labels separated by " | " (e.g. "AI prospecting | CRM sync | Intent signals | Analytics")
- "Marketing Messaging": ONE sentence (max ~25 words) on their core positioning. Punchy, no filler.
- "SEO": ONE short sentence on their content/SEO strategy. If no visible blog or content, write exactly "insufficient data — no visible blog/content". Never pad.

Be terse. If a field's data isn't on the page, write "insufficient data" — do NOT pad with speculation.
Return only valid JSON."""

    try:
        resp = client.messages.create(
            model=CLAUDE_MODEL, max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        data = json.loads(_strip_json_fence(resp.content[0].text))
        # SEO also uses blog text if scraped separately
        if blog_t and not data.get("SEO"):
            try:
                resp2 = client.messages.create(
                    model=CLAUDE_MODEL, max_tokens=150,
                    messages=[{"role": "user", "content": f"""Describe {company_name}'s SEO strategy in ONE short sentence (max ~20 words) based on this blog content. What topics they cover + how active the blog is. If the content is too thin to tell, write exactly "insufficient data".

Blog content:
{blog_t[:1500]}

Return JSON: {{"seo": "one short sentence"}}
Return only valid JSON."""}],
                )
                data["SEO"] = json.loads(_strip_json_fence(resp2.content[0].text)).get("seo", "")
            except Exception:
                pass
        data["Top Logos"] = top_logos
        return data
    except Exception as e:
        print(f"    Product info extraction failed: {e}")
        return {**empty, "Top Logos": top_logos}


# ---------------------------------------------------------------------------
# Step 9: Customer reviews
# ---------------------------------------------------------------------------

def _slug_matches_company(slug: str, company_name: str) -> bool:
    """True if a G2 product slug plausibly belongs to `company_name`.

    Conservative: accepts when the compacted names are substrings of each other
    (``notion`` ⊂ ``notion``, ``hubspot`` ⊂ ``hubspot-crm``) or share a
    meaningful (len≥4) token. Rejects near-miss strangers like ``goco`` for
    "Gojiberry" or ``altair-monarch`` for "Monaco", so their reviews aren't
    misattributed.
    """
    def _compact(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", s.lower())

    ck, sk = _compact(company_name), _compact(slug)
    if not ck or not sk:
        return False
    if ck in sk or sk in ck:
        return True
    ctoks = {t for t in re.split(r"[^a-z0-9]+", company_name.lower()) if len(t) >= 4}
    stoks = {t for t in re.split(r"[^a-z0-9]+", slug.lower()) if len(t) >= 4}
    return bool(ctoks & stoks)


def get_customer_reviews(
    company_name: str,
    website: str,
    client: anthropic.Anthropic,
) -> str:
    # Find G2 URL — but only trust it if the product slug plausibly matches the
    # company. Exa's site:g2.com search happily returns the *nearest* product
    # when the company has no G2 page (e.g. "Gojiberry" → g2.com/products/goco,
    # "Monaco" → altair-monarch), which would otherwise attribute a stranger's
    # reviews to this competitor. Reject non-matching slugs and fall through.
    g2_url = ""
    try:
        result = search_web(
            query=f"{company_name} reviews site:g2.com",
            num_results=3,
            summary_question=f"What is the G2 review page URL for {company_name}?",
            include_summary=False,  # we only read result URLs here
        )
        for r in result.get("results", []):
            url = r.get("url", "")
            if "g2.com/products/" in url:
                slug = url.split("g2.com/products/", 1)[1].split("/")[0]
                if _slug_matches_company(slug, company_name):
                    g2_url = url.split("?")[0]
                    break
    except Exception:
        pass

    # Trustpilot fallback
    trustpilot_url = ""
    if not g2_url and website:
        domain = _strip_www(urlparse(website).netloc)
        trustpilot_url = f"https://www.trustpilot.com/review/{domain}"

    platform   = "g2" if g2_url else ("trustpilot" if trustpilot_url else "")
    review_url = g2_url or trustpilot_url
    if not review_url:
        return ""

    try:
        result = _review_mod.scrape_reviews(
            platform=platform,
            product_url=review_url,
            max_reviews=10,
        )
        rating  = result.get("overall_rating", "")
        reviews = result.get("reviews", [])
        if not rating and not reviews:
            return ""

        positives, negatives = [], []
        for r in reviews[:10]:
            txt   = (r.get("text") or r.get("review_text") or "").strip()
            stars = r.get("star_rating") or r.get("rating") or 0
            try:
                stars = float(str(stars))
            except Exception:
                stars = 0
            if stars >= 4:
                positives.append(txt[:200])
            elif stars > 0:
                negatives.append(txt[:200])

        if not positives and not negatives:
            return f"{rating}/5" if rating else ""

        resp = client.messages.create(
            model=CLAUDE_MODEL, max_tokens=200,
            messages=[{"role": "user", "content": f"""Summarise {company_name}'s customer reviews in one line.
Overall rating: {rating}/5

Positive reviews:
{chr(10).join(positives[:5])}

Negative reviews:
{chr(10).join(negatives[:5])}

Format: "[rating]/5 — [what customers love] | [top complaint]"
Example: "4.3/5 — Customers love ease of setup and AI suggestions | Main complaints are limited CRM integrations and high price"

Return JSON: {{"reviews": "..."}}
Return only valid JSON."""}],
        )
        return json.loads(_strip_json_fence(resp.content[0].text)).get("reviews", f"{rating}/5")
    except Exception as e:
        print(f"    Reviews failed ({platform}): {e}")
        return ""


# ---------------------------------------------------------------------------
# Step 10: Deal size
# ---------------------------------------------------------------------------

def get_deal_size(company_name: str, website: str, client: anthropic.Anthropic) -> str:
    domain = _strip_www(urlparse(website).netloc) if website else ""
    label = f"{company_name} ({domain})" if domain else company_name
    query = (
        f'"{domain}" {company_name} average deal size ACV annual contract value pricing enterprise mid-market'
        if domain else
        f"{company_name} average deal size ACV annual contract value pricing enterprise mid-market"
    )
    try:
        result = search_web(
            query=query,
            num_results=3,
            summary_question=f"What is the average deal size or ACV for {label}?",
        )
    except Exception:
        return "not available"

    snippets = []
    for r in result.get("results", []):
        if r.get("summary"):
            snippets.append(r["summary"])
        snippets.extend(r.get("highlights", []))
    if not snippets:
        return "not available"

    try:
        resp = client.messages.create(
            model=CLAUDE_MODEL, max_tokens=100,
            messages=[{"role": "user", "content": f"""Find the average deal size or ACV for {label}.

Research:
{chr(10).join(_cap(snippets, 6))}

If found, write concisely (e.g. "$15k/year", "$5k–$50k", "enterprise $100k+").
If not found, write "not available".

Return JSON: {{"deal_size": "..."}}
Return only valid JSON."""}],
        )
        return json.loads(_strip_json_fence(resp.content[0].text)).get("deal_size", "not available")
    except Exception:
        return "not available"


# ---------------------------------------------------------------------------
# Step 11: Content type
# ---------------------------------------------------------------------------

def get_content_type(
    company_name: str,
    founder_post_summaries: List[str],
    scraped: dict,
    client: anthropic.Anthropic,
) -> str:
    blog_t = ""
    for key, text in scraped.get("full_text_by_page", {}).items():
        if any(kw in key.lower() for kw in ["blog", "resource", "insight", "article", "content"]):
            blog_t = text[:2000]
            break

    posts_block = "\n---\n".join(founder_post_summaries) if founder_post_summaries else "No founder posts available."

    try:
        resp = client.messages.create(
            model=CLAUDE_MODEL, max_tokens=200,
            messages=[{"role": "user", "content": f"""Summarise {company_name}'s content strategy in ONE short sentence (max ~25 words). Cover topics + formats + how active they are.

If both founder content and blog are absent or too thin to judge, write exactly "insufficient data". Do not pad. Do not speculate.

Founder content style:
{posts_block}

Company blog sample:
{blog_t[:1500] or "Not available."}

Return JSON: {{"content_type": "one short sentence"}}
Return only valid JSON."""}],
        )
        return json.loads(_strip_json_fence(resp.content[0].text)).get("content_type", "")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Step 12: Final analysis
# ---------------------------------------------------------------------------

def analyze_competitor(
    company_name: str,
    profile: Dict[str, str],
    icp_context: str,
    client: anthropic.Anthropic,
) -> Dict[str, str]:
    profile_block = "\n".join(f"- {k}: {v}" for k, v in profile.items() if v)

    # Our ICP/positioning + the JSON spec are identical for every competitor in a
    # run, so cache them in the system prefix; only the target profile varies.
    system = f"""You are a competitive intelligence analyst. Analyze the target company from the perspective of a competing product.

Our product context (ICP / positioning):
{icp_context}

Return JSON with:
- "Competitor Score": score out of 5 as a string (e.g. "3.5", "4.0"). Base it on funding, market traction, product depth, and GTM execution relative to our product.
- "Strength": MAX 2 short lines (~30 words total) on their key competitive advantages. Punchy, specific, grounded in the data provided. No preamble, no hedging. If genuinely no signal, write "insufficient data".
- "Weakness": MAX 2 short lines (~30 words total) on their key gaps relative to our positioning. Punchy, specific. If genuinely no signal, write "insufficient data".
- "Target ICP": who they sell to. Use one or more of these exact categories separated by " + ": "SMB", "Mid-Market", "Enterprise", "Early-Stage Startups", "All". E.g. "Mid-Market + Enterprise".

Return only valid JSON."""

    prompt = f"""Analyze {company_name} from the perspective of our competing product.

{company_name} profile:
{profile_block}"""

    try:
        resp = client.messages.create(
            model=CLAUDE_MODEL, max_tokens=600,
            system=cached_system(system),
            messages=[{"role": "user", "content": prompt}],
        )
        return json.loads(_strip_json_fence(resp.content[0].text))
    except Exception as e:
        print(f"    Final analysis failed: {e}")
        return {"Competitor Score": "", "Strength": "", "Weakness": "", "Target ICP": ""}

