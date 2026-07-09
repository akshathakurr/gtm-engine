"""Per-company / per-lead steps for the email outreach workflow.

Company enrichment, scoring, buyer discovery, persona classification, Apollo
email lookup, small talk, post scraping/filtering, personalisation hooks, and
email copy. Each function takes plain inputs and returns raw values;
orchestration and sheet/CSV I/O live in ``workflow.py``.
"""

import json
from typing import List, Dict

import anthropic

from config import CLAUDE_MODEL
from workflows._common import strip_json_fence as _strip_json_fence, map_rate_limited
from scrapers.web_search.scraper import search_web
from scrapers.contact_finder import scraper as _contact_scraper
from scrapers.linkedin_profile_post_scraper import scraper as _post_scraper

try:
    from scrapers.small_talk_scraper import scraper as _small_talk_scraper  # type: ignore
    _SMALL_TALK_AVAILABLE = True
except Exception:
    _small_talk_scraper = None  # type: ignore
    _SMALL_TALK_AVAILABLE = False

try:
    from skills.personalisation_hook import skill as _personalisation_skill  # type: ignore
    _PERSONALISATION_AVAILABLE = True
except Exception:
    _personalisation_skill = None  # type: ignore
    _PERSONALISATION_AVAILABLE = False

try:
    from skills.email_copy_writer import skill as _email_copy_skill  # type: ignore
    _EMAIL_COPY_AVAILABLE = True
except Exception:
    _email_copy_skill = None  # type: ignore
    _EMAIL_COPY_AVAILABLE = False


# Concurrency. Per-lead compute (Claude + Exa) runs in a bounded pool; the
# backend is written sequentially on the main thread afterwards, so it is never
# touched concurrently. Throttled services use a rate limiter that spaces call
# *starts* by the same intervals the old serial sleeps used.
ENRICH_CONCURRENCY = 6        # Claude/Exa per-lead steps
APOLLO_MIN_INTERVAL = 1       # seconds between Apollo email lookups (step 6)
APOLLO_CONCURRENCY = 3
POSTS_MIN_INTERVAL = 5        # seconds between profile-posts runs (step 8)
POSTS_CONCURRENCY = 3
# Score/classify run one LLM call per chunk of companies (not one call for the
# whole sheet). Chunking prevents (a) JSON truncation on large sheets — 1000
# rows overrun max_tokens and later rows come back blank — and (b) batch-context
# drift, where the same company is scored differently depending on its neighbours.
LLM_BATCH_SIZE = 40


# ---------------------------------------------------------------------------
# Step 1 — Enrich company
# ---------------------------------------------------------------------------

def enrich_company(
    company_name: str,
    fields: List[Dict[str, str]],
    client: anthropic.Anthropic,
) -> Dict[str, str]:
    empty = {f["key"]: "" for f in fields}
    try:
        search_result = search_web(
            query=(f"{company_name} company official website linkedin employees revenue funding "
                   f"headquarters founded year competitors"),
            num_results=5,
            summary_question=(
                f"What is {company_name}'s official website URL, LinkedIn company page URL, "
                f"employee count, estimated annual revenue, year founded, total funding raised, "
                f"headquarters location, and 2-3 immediate direct competitors?"
            ),
        )
    except Exception as e:
        print(f"    Web search failed for {company_name}: {e}")
        return empty

    snippets = []
    for r in search_result.get("results", []):
        if r.get("summary"):
            snippets.append(r["summary"])
        snippets.extend(r.get("highlights", []))

    if not snippets:
        return empty

    field_block = "\n".join(f'- "{f["key"]}": {f["desc"]}' for f in fields)

    prompt = f"""Extract company information for "{company_name}" from the research below.

Research:
{chr(10).join(snippets[:20])}

Required fields (use empty string if not found):
{field_block}

Return a JSON object with exactly these keys: {list(empty.keys())}.

Formatting rules — apply strictly:
- company_url: canonical homepage URL (e.g. https://acme.com)
- company_linkedin: full LinkedIn company page URL (https://www.linkedin.com/company/...)
- company_description: one short, to-the-point line about what the company does
- employee_count: integer or range as stated (e.g. "215", "5,500+")
- founded_year: 4-digit year only (e.g. "2014")
- total_funding: absolute number with capital M for millions or B for billions, no "~", no "$".
  Examples: "110M", "4M", "164.12M", "1.2B". If not found use "".
- est_revenue: same format ("15M", "1.4M"). All values are USD — never include "$".
  If you cannot find a credible figure, return "Not available".
- hq: city name only — no state, no country, no street.
  Examples: "Oakland", "Boston", "San Francisco". If only the country is known, return that.
- competitors: 2-3 immediate direct competitor company names, comma-separated.
  Pick the most directly comparable companies — not adjacent categories. Names only.
  Example: "Acme, Globex, Initech"

Return only valid JSON, no explanation."""

    try:
        resp = client.messages.create(
            model=CLAUDE_MODEL, temperature=0, max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        return json.loads(_strip_json_fence(resp.content[0].text))
    except Exception as e:
        print(f"    LLM extraction failed for {company_name}: {e}")
        return empty


# ---------------------------------------------------------------------------
# Step 2 — Score (ICP Segment + Priority + Reasoning)
# ---------------------------------------------------------------------------

def score_companies(
    leads: List[Dict],
    icp_context: str,
    client: anthropic.Anthropic,
) -> List[Dict[str, str]]:
    """Score every company on ICP Segment + Priority. Person info is not used here."""
    def _score_chunk(chunk: List[Dict]) -> List[Dict[str, str]]:
        leads_block = "\n".join(
            (
                f"{i+1}. {l['company']}"
                + (f" | {l.get('company_description', '')}" if l.get("company_description") else "")
                + (f" | Employees: {l['employee_count']}" if l.get("employee_count") else "")
                + (f" | Revenue: {l['est_revenue']}" if l.get("est_revenue") else "")
                + (f" | Funding: {l['total_funding']}" if l.get("total_funding") else "")
                + (f" | Founded: {l['founded_year']}" if l.get("founded_year") else "")
                + (f" | HQ: {l['hq']}" if l.get("hq") else "")
            )
            for i, l in enumerate(chunk)
        )

        prompt = f"""You are a GTM analyst prioritizing companies for outbound email outreach.

ICP Context:
{icp_context}

Priority tiers (use ICP if filled; fall back to general fit signals if empty):
- P0: Best-fit accounts. Match ICP tightly. Reach out first.
- P1: Good fit with some gaps. Worth pursuing.
- P2: Weak fit or too early. Lower priority.

ICP segments are defined in the ICP Context above. If named segments exist
(e.g. "Series-A AI infra", "Mid-market fintech"), assign each company to the
best-fitting segment. If none are defined, return "" for icp_segment.

Companies:
{leads_block}

For each company, return:
- index (1-based)
- icp_segment (one of the named segments, or "")
- priority (P0/P1/P2)
- reasoning (ONE plain sentence — to the point, no bullets, no filler — explaining the priority decision a salesperson can act on)

Return only valid JSON, no explanation."""

        resp = client.messages.create(
            model=CLAUDE_MODEL, temperature=0, max_tokens=4000,
            messages=[{"role": "user", "content": prompt}],
        )
        parsed = json.loads(_strip_json_fence(resp.content[0].text))
        out: List[Dict[str, str]] = [
            {"priority": "", "icp_segment": "", "reasoning": ""} for _ in chunk
        ]
        for item in parsed:
            idx = item.get("index", 0) - 1
            if 0 <= idx < len(chunk):
                out[idx] = {
                    "priority":    item.get("priority", ""),
                    "icp_segment": item.get("icp_segment", ""),
                    "reasoning":   item.get("reasoning", ""),
                }
        return out

    # One call per chunk — avoids JSON truncation on large sheets and keeps each
    # company scored against only its chunk. Chunks are independent → run in parallel.
    chunks = [leads[i:i + LLM_BATCH_SIZE] for i in range(0, len(leads), LLM_BATCH_SIZE)]
    chunk_results, _ = map_rate_limited(_score_chunk, chunks, max_workers=ENRICH_CONCURRENCY)
    result: List[Dict[str, str]] = []
    for chunk, res in zip(chunks, chunk_results):
        if res is not None:
            result.extend(res)
        else:
            result.extend({"priority": "", "icp_segment": "", "reasoning": ""} for _ in chunk)
    return result


# ---------------------------------------------------------------------------
# Step 4 — Find buyer at company
# ---------------------------------------------------------------------------

def find_buyer_at_company(
    company_name: str,
    icp_context: str,
    client: anthropic.Anthropic,
) -> Dict[str, str]:
    """Web-search and pick the most relevant buyer per ICP DM titles."""
    empty = {"name": "", "position": "", "linkedin": ""}
    try:
        result = search_web(
            query=f"{company_name} founder CEO CTO head of engineering leadership team",
            num_results=5,
            summary_question=(
                f"Who are the founders, CEO, and senior leaders at {company_name}? "
                f"List their full names, titles, and LinkedIn URLs if available."
            ),
        )
    except Exception as e:
        print(f"    Buyer search failed for {company_name}: {e}")
        return empty

    snippets = []
    for r in result.get("results", []):
        if r.get("summary"):
            snippets.append(r["summary"])
        snippets.extend(r.get("highlights", []))

    if not snippets:
        return empty

    prompt = f"""From the ICP context, identify the single most relevant buyer to target at {company_name}.

ICP Context (focus on Decision Maker / buyer persona titles — usually founders or specified roles):
{icp_context}

Research about {company_name}'s leadership:
{chr(10).join(snippets[:15])}

Return a JSON object with the best person to target:
{{"name": "Full Name", "position": "Their Title", "linkedin": "https://www.linkedin.com/in/... or empty string"}}

Return only valid JSON, no explanation."""

    try:
        resp = client.messages.create(
            model=CLAUDE_MODEL, temperature=0, max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        return json.loads(_strip_json_fence(resp.content[0].text))
    except Exception as e:
        print(f"    Buyer extraction failed for {company_name}: {e}")
        return empty


def find_linkedin_url(name: str, company: str, client: anthropic.Anthropic) -> str:
    """Search for a person's LinkedIn profile URL when only name+company are known."""
    try:
        result = search_web(
            query=f"{name} {company} site:linkedin.com/in",
            num_results=3,
            summary_question=f"What is the LinkedIn profile URL for {name} at {company}?",
        )
    except Exception as e:
        print(f"    LinkedIn URL search failed for {name}: {e}")
        return ""

    for r in result.get("results", []):
        url = r.get("url", "")
        if "linkedin.com/in/" in url:
            return url

    snippets = []
    for r in result.get("results", []):
        if r.get("summary"):
            snippets.append(r["summary"])
        snippets.extend(r.get("highlights", []))
    if not snippets:
        return ""

    try:
        resp = client.messages.create(
            model=CLAUDE_MODEL, temperature=0, max_tokens=100,
            messages=[{"role": "user", "content": (
                f"Extract the LinkedIn profile URL for {name} at {company} from this text.\n\n"
                + "\n".join(snippets[:8])
                + "\n\nReturn JSON: {\"linkedin_url\": \"https://linkedin.com/in/...\" or \"\"}\nReturn only valid JSON."
            )}],
        )
        data = json.loads(_strip_json_fence(resp.content[0].text))
        return data.get("linkedin_url", "")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Step 5 — Classify buyer persona
# ---------------------------------------------------------------------------

def classify_personas(
    leads: List[Dict],
    icp_context: str,
    add_personas: List[str],
    remove_personas: List[str],
    client: anthropic.Anthropic,
) -> List[str]:
    overrides = ""
    if add_personas:
        overrides += f"\nFor this run only, also treat these titles/roles as buyers: {', '.join(add_personas)}"
    if remove_personas:
        overrides += f"\nFor this run only, exclude these titles/roles: {', '.join(remove_personas)}"

    def _classify_chunk(chunk: List[Dict]) -> List[str]:
        leads_block = "\n".join(
            f"{i+1}. Name: {l['name']} | Title: {l['position']} | Company: {l['company']}"
            for i, l in enumerate(chunk)
        )
        prompt = f"""You are a B2B sales analyst classifying leads by buyer role.

ICP / Buyer Persona Context:
{icp_context}
{overrides}

Definitions (use ICP context to map titles; fall back to general B2B conventions if ICP is empty):
- Decision Maker: Has budget authority and can sign/approve a deal.
- Champion: Influences the buying decision but cannot sign alone.
- Non Decision Maker: Not involved in the buying decision.

Leads:
{leads_block}

Return a JSON array — one object per lead — with "index" (1-based) and "classification".
Only return valid JSON, no explanation."""

        resp = client.messages.create(
            model=CLAUDE_MODEL, temperature=0, max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        parsed = json.loads(_strip_json_fence(resp.content[0].text))
        out = [""] * len(chunk)
        for item in parsed:
            idx = item.get("index", 0) - 1
            if 0 <= idx < len(chunk):
                out[idx] = item.get("classification", "")
        return out

    # One call per chunk — no truncation, each lead judged against only its chunk.
    chunks = [leads[i:i + LLM_BATCH_SIZE] for i in range(0, len(leads), LLM_BATCH_SIZE)]
    chunk_results, _ = map_rate_limited(_classify_chunk, chunks, max_workers=ENRICH_CONCURRENCY)
    result: List[str] = []
    for chunk, res in zip(chunks, chunk_results):
        result.extend(res if res is not None else [""] * len(chunk))
    return result


# ---------------------------------------------------------------------------
# Step 6 — Find email via Apollo
# ---------------------------------------------------------------------------

def find_email(lead: Dict) -> str:
    name = lead.get("name", "")
    parts = name.strip().split(" ", 1)
    first = parts[0] if parts else ""
    last  = parts[1] if len(parts) > 1 else ""
    try:
        result = _contact_scraper.find_contact(
            first_name=first or None,
            last_name=last or None,
            organization_name=lead.get("company") or None,
            linkedin_url=lead.get("linkedin") or None,
        )
        if result.get("found") and result.get("person"):
            return result["person"].get("email", "")
    except Exception as e:
        print(f"    Contact finder failed for {name}: {e}")
    return ""


# ---------------------------------------------------------------------------
# Step 7 — Small talk (stub-aware)
# ---------------------------------------------------------------------------

def scrape_small_talk(profile_url: str, name: str, company: str) -> str:
    if not _SMALL_TALK_AVAILABLE:
        return ""
    try:
        result = _small_talk_scraper.scrape_small_talk(
            profile_url=profile_url, name=name, company=company,
        )
        return result.get("small_talk", "")
    except Exception as e:
        print(f"    Small talk failed for {name}: {e}")
        return ""


# ---------------------------------------------------------------------------
# Step 8 — Posts
# ---------------------------------------------------------------------------

def scrape_and_filter_posts(
    profile_url: str,
    icp_context: str,
    max_posts: int,
    days_back: int,
    client: anthropic.Anthropic,
) -> Dict:
    empty = {"urls": [], "posts_data": []}

    if not profile_url:
        return empty

    try:
        result = _post_scraper.scrape_linkedin_profile_posts(
            profile_url=profile_url,
            max_posts=max_posts,
            days_back=days_back,
        )
    except Exception as e:
        print(f"    Post scraping failed for {profile_url}: {e}")
        return empty

    posts = result.get("posts", [])
    if not posts:
        return empty

    posts_block = "\n".join(
        f"{i+1}. [{p.get('posted_at', '')[:10]}] {(p.get('text') or '')[:300]}"
        for i, p in enumerate(posts)
    )

    prompt = f"""You are evaluating LinkedIn posts to identify which ones are relevant for email sales outreach.

ICP Context (pay attention to the 'LinkedIn Post Relevance Filter' section if present):
{icp_context}

Posts (newest first):
{posts_block}

Task: Return the 1-based indices of posts that match the relevance criteria in the ICP.
- If the ICP has no post criteria defined, return all post indices (assume all are relevant).
- Prefer posts where the person shares opinions, challenges, or experiences related to our product area.
- Exclude purely promotional reposts, congratulations posts, or generic announcements.

Return a JSON array of integers: [1, 3, 4]
If none match, return: []
Return only valid JSON, no explanation."""

    try:
        resp = client.messages.create(
            model=CLAUDE_MODEL, temperature=0, max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        matching = json.loads(_strip_json_fence(resp.content[0].text))
    except Exception as e:
        print(f"    Post filtering failed: {e}")
        matching = list(range(1, len(posts) + 1))

    valid = [i for i in matching if isinstance(i, int) and 1 <= i <= len(posts)]
    matched_posts = [posts[i - 1] for i in valid]
    return {
        "urls": [p["url"] for p in matched_posts if p.get("url")],
        "posts_data": [
            {"url": p.get("url", ""), "text": p.get("text", ""), "posted_at": p.get("posted_at", "")}
            for p in matched_posts
        ],
    }


# ---------------------------------------------------------------------------
# Steps 9-10 — Hooks + Copy (stub-aware)
# ---------------------------------------------------------------------------

def generate_personalisation_hooks(
    name: str, company: str, position: str,
    matching_posts: List[Dict], small_talk: str, icp_context: str,
    competitors: str = "", company_description: str = "",
    employee_count: str = "", est_revenue: str = "",
    total_funding: str = "", hq: str = "",
) -> str:
    if not _PERSONALISATION_AVAILABLE:
        return ""
    competitor_list = [c.strip() for c in (competitors or "").split(",") if c.strip()]
    try:
        result = _personalisation_skill.generate_hooks(
            name=name, company=company, position=position,
            matching_posts=matching_posts, small_talk=small_talk, icp_context=icp_context,
            competitors=competitor_list, company_description=company_description,
            employee_count=employee_count, est_revenue=est_revenue,
            total_funding=total_funding, hq=hq,
        )
        hooks = result.get("hooks", "")
        skill_errors = result.get("errors") or []
        if skill_errors and not hooks:
            print(f"    No hook for {name}: {'; '.join(skill_errors)}")
        return hooks
    except Exception as e:
        print(f"    Personalisation hook failed for {name}: {e}")
        return ""


def write_email_copy(
    name: str, company: str, position: str, email: str,
    buyer_persona: str, priority: str,
    matching_posts: List[Dict], small_talk: str, personalisation_hook: str,
    icp_context: str,
    employee_count: str, est_revenue: str, total_funding: str, hq: str, competitors: str,
) -> str:
    if not _EMAIL_COPY_AVAILABLE:
        return ""
    try:
        result = _email_copy_skill.write_copy(
            name=name, company=company, position=position, email=email,
            buyer_persona=buyer_persona, priority=priority,
            matching_posts=matching_posts, small_talk=small_talk,
            personalisation_hook=personalisation_hook, icp_context=icp_context,
            employee_count=employee_count, est_revenue=est_revenue,
            total_funding=total_funding, hq=hq, competitors=competitors,
        )
        return result.get("copy", "")
    except Exception as e:
        print(f"    Email copy writer failed for {name}: {e}")
        return ""
