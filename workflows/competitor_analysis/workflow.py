"""
Competitor Analysis Workflow — Deep competitive intelligence per competitor.

Reads competitor names + URLs from the 'Bird Eye' sheet tab and fills all columns.
Other tabs (Company, Founder, GTM, Product) auto-populate via sheet formulas.

Steps per competitor:
  1  — Website scrape (free — done first, reused across all later steps)
  2  — Company LinkedIn URL (from scrape, web search fallback)
  3  — Company Description (one-liner from scrape)
  4  — Firmographics via Exa (headcount exact, year, funding stage, total funding, revenue, HQ city)
  5  — Recent news via Exa (one-liner + source URL per item)
  6  — Founders (name, LinkedIn, Twitter) via web search
  7  — Founder post types via LinkedIn post scraper + Twitter scraper
  8  — Product info from website scrape (persona, sales motion, CTA, pricing, features, logos, messaging, SEO)
  9  — Customer reviews via Review Scraper (G2 → Trustpilot fallback)
  10 — Deal size via web search
  11 — Content type synthesis from founder posts + company blog
  12 — Final Claude analysis (Target ICP categorized, Competitor Score /5, Strengths, Weaknesses)

Usage:
  python -m workflows.competitor_analysis.workflow --sheet-id SHEET_ID
  python -m workflows.competitor_analysis.workflow --sheet-id SHEET_ID --sheet-name "Bird Eye"
  python -m workflows.competitor_analysis.workflow --input-csv competitors.csv
  python -m workflows.competitor_analysis.workflow --sheet-id SHEET_ID --skip-reviews --skip-twitter
  python -m workflows.competitor_analysis.workflow --sheet-id SHEET_ID --auto       # CI / cron
"""

import os
import sys
import csv
import json
import time
import argparse
import re
from typing import Optional, List, Dict
from urllib.parse import urlparse

import anthropic

import subprocess

from config import CLAUDE_MODEL, CONTEXT_DIR
from scrapers.web_search.scraper import search_web
from scrapers.website_scraper import scraper as _website_mod
from scrapers.linkedin_profile_post_scraper import scraper as _li_posts_mod
from scrapers.twitter_profile_scraper import scraper as _twitter_mod
from scrapers.review_scraper import scraper as _review_mod

import threading
from concurrent.futures import ThreadPoolExecutor


# ---------------------------------------------------------------------------
# Inlined helpers (kept here so each workflow is self-contained)
# ---------------------------------------------------------------------------

# Each competitor's independent enrichment lookups (Claude + Exa) run together
# in a bounded pool. Competitors themselves stay sequential, so throttled Apify
# actors and the single-threaded sheet/CSV backend are never hit concurrently.
COMPETITOR_ENRICH_CONCURRENCY = 6


def _run_parallel(tasks: Dict[str, "callable"], max_workers: int = COMPETITOR_ENRICH_CONCURRENCY) -> Dict[str, tuple]:
    """Run named zero-arg thunks concurrently. Returns {key: (result, error)}.

    Exceptions are captured per task (never raised), so one failed lookup can't
    abort the others or the competitor.
    """
    out: Dict[str, tuple] = {}
    if not tasks:
        return out
    lock = threading.Lock()

    def run(key, fn):
        try:
            value, err = fn(), None
        except Exception as e:  # noqa: BLE001 — captured per task, surfaced to caller
            value, err = None, e
        with lock:
            out[key] = (value, err)

    with ThreadPoolExecutor(max_workers=min(max_workers, len(tasks))) as ex:
        for fut in [ex.submit(run, k, fn) for k, fn in tasks.items()]:
            fut.result()
    return out

def gws_read_sheet(sheet_id: str, sheet_name: str) -> List[List[str]]:
    result = subprocess.run(
        ["gws", "sheets", "spreadsheets", "values", "get",
         "--params", json.dumps({"spreadsheetId": sheet_id, "range": sheet_name})],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout).get("values", [])


def gws_write_range(sheet_id: str, range_: str, values: List[List[str]]) -> None:
    subprocess.run(
        ["gws", "sheets", "spreadsheets", "values", "update",
         "--params", json.dumps({"spreadsheetId": sheet_id, "range": range_,
                                  "valueInputOption": "USER_ENTERED"}),
         "--json", json.dumps({"values": values})],
        capture_output=True, text=True, check=True,
    )


def col_letter(idx: int) -> str:
    result, n = "", idx + 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        result = chr(65 + rem) + result
    return result


def find_col(headers: List[str], *names: str) -> Optional[int]:
    lower = [n.lower() for n in names]
    for i, h in enumerate(headers):
        if h.strip().lower() in lower:
            return i
    return None


def ensure_col(headers: List[str], name: str) -> int:
    idx = find_col(headers, name)
    if idx is not None:
        return idx
    headers.append(name)
    return len(headers) - 1


def cell(row: List[str], idx: Optional[int]) -> str:
    return row[idx].strip() if idx is not None and idx < len(row) else ""


def load_icp(*_args, **_kwargs) -> str:
    """Concatenate all .md files in context/ (excluding .example templates)."""
    parts = []
    for fname in sorted(os.listdir(CONTEXT_DIR)):
        if fname.endswith(".md") and ".example" not in fname:
            with open(os.path.join(CONTEXT_DIR, fname)) as f:
                parts.append(f.read())
    if not parts:
        raise FileNotFoundError(
            f"No context .md files found in {CONTEXT_DIR}. "
            f"Copy context/context.md.example to context/context.md and fill it in."
        )
    return "\n\n---\n\n".join(parts)


def _strip_json_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1]).strip()
    # Tolerate prose preamble/suffix around the JSON: carve out the outermost
    # object or array. Models sometimes reason in prose before emitting JSON.
    if text and text[0] not in "{[":
        starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
        ends = [i for i in (text.rfind("}"), text.rfind("]")) if i != -1]
        if starts and ends and max(ends) > min(starts):
            text = text[min(starts):max(ends) + 1].strip()
    return text


# ---------------------------------------------------------------------------
# Backends — same interface for Google Sheets and CSV
# ---------------------------------------------------------------------------

class GoogleSheetsBackend:
    def __init__(self, sheet_id: str, sheet_name: str):
        self.sheet_id = sheet_id
        self.sheet_name = sheet_name

    def read_all(self) -> List[List[str]]:
        return gws_read_sheet(self.sheet_id, self.sheet_name)

    def write_header(self, col_idx: int, name: str) -> None:
        gws_write_range(self.sheet_id, f"{self.sheet_name}!{col_letter(col_idx)}1", [[name]])

    def write_cell(self, row_num: int, col_idx: int, value: str) -> None:
        gws_write_range(self.sheet_id, f"{self.sheet_name}!{col_letter(col_idx)}{row_num}", [[value]])


class CsvBackend:
    """In-memory rows; rewrites the CSV after every write so partial progress survives crashes."""

    def __init__(self, input_path: str, output_path: Optional[str] = None):
        self.input_path = input_path
        self.output_path = output_path or input_path
        self.rows: List[List[str]] = []

    def read_all(self) -> List[List[str]]:
        if not os.path.exists(self.input_path):
            raise FileNotFoundError(f"Input CSV not found: {self.input_path}")
        with open(self.input_path, newline="", encoding="utf-8") as f:
            self.rows = [list(row) for row in csv.reader(f)]
        return self.rows

    def _ensure(self, row_idx: int, col_idx: int) -> None:
        while len(self.rows) <= row_idx:
            self.rows.append([])
        while len(self.rows[row_idx]) <= col_idx:
            self.rows[row_idx].append("")

    def _flush(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.output_path)) or ".", exist_ok=True)
        width = max((len(r) for r in self.rows), default=0)
        for r in self.rows:
            while len(r) < width:
                r.append("")
        with open(self.output_path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(self.rows)

    def write_header(self, col_idx: int, name: str) -> None:
        self._ensure(0, col_idx)
        self.rows[0][col_idx] = name
        self._flush()

    def write_cell(self, row_num: int, col_idx: int, value: str) -> None:
        idx = row_num - 1
        self._ensure(idx, col_idx)
        self.rows[idx][col_idx] = value
        self._flush()


# ---------------------------------------------------------------------------
# Empty-context guard — flag missing context.md sections before doing work
# ---------------------------------------------------------------------------

# Sections in context.md that materially affect the final analysis prompt.
# If any are missing/empty, the LLM scoring/strength/weakness output gets vague.
RECOMMENDED_CONTEXT_SECTIONS = [
    "Product",
    "Ideal Customer Profile",
    "Competitors",
]


def _section_body(text: str, header: str) -> str:
    if not text:
        return ""
    pattern = rf"(?ms)^##\s+{re.escape(header)}\s*$\n(.*?)(?=^##\s+|\Z)"
    m = re.search(pattern, text)
    if not m:
        return ""
    section = m.group(1)
    ans = re.search(r"(?ms)^###\s+Answer\s*$\n(.*?)(?=^###\s+|\Z)", section)
    body = ans.group(1) if ans else section
    body = "\n".join(ln for ln in body.splitlines() if not ln.strip().startswith("<!--")).strip()
    if body.lower() in ("(fill this in)", "(none)", "(skip)"):
        return ""
    # Treat a body that's only template scaffolding ("- **X:**" with no values) as empty
    stripped = "\n".join(
        ln for ln in body.splitlines()
        if ln.strip() and not re.match(r"^[-*\s]*\*\*[^*]+:\*\*\s*$", ln.strip())
    ).strip()
    return stripped


def check_context_complete(auto: bool) -> None:
    """Warn (or under --auto, abort) if recommended context.md sections are empty."""
    path = os.path.join(CONTEXT_DIR, "context.md")
    text = ""
    if os.path.exists(path):
        with open(path) as f:
            text = f.read()

    missing = [h for h in RECOMMENDED_CONTEXT_SECTIONS if not _section_body(text, h)]
    if not missing:
        return

    print("\n" + "=" * 70)
    print(" Heads up — context/context.md is missing (or empty for) these sections:")
    print("=" * 70)
    for h in missing:
        print(f"  · ## {h}")
    print("\nThese power the final competitor analysis (score, strengths, weaknesses,")
    print("Target ICP). Without them the analysis will be vague and generic.")
    print("See context/context.md.example for the full template.\n")

    if auto:
        print("ERROR: --auto specified — aborting. Fill those sections and re-run.")
        sys.exit(2)

    choice = input("Continue anyway? [y/N] ").strip().lower()
    if choice not in ("y", "yes"):
        print("Aborted. Fill context/context.md and re-run.")
        sys.exit(0)
    print()


# ---------------------------------------------------------------------------
# Column definitions — exact Bird Eye header names (Notes is manual, excluded)
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Step 1: Website scrape
# ---------------------------------------------------------------------------

def scrape_website(url: str) -> dict:
    if not url:
        return {}
    try:
        return _website_mod.scrape_website(url=url)
    except Exception as e:
        print(f"    Website scrape error: {e}")
        return {}


# ---------------------------------------------------------------------------
# Step 2: Company LinkedIn URL
# ---------------------------------------------------------------------------

def find_linkedin_url(company_name: str, website: str, scraped: dict,
                      client: anthropic.Anthropic) -> str:
    """Extract LinkedIn company URL by fetching the homepage via Jina Reader
    (which includes href link URLs in its markdown output). Falls back to web search."""
    import requests as _requests

    pattern = r'https?://(?:[\w-]+\.)?linkedin\.com/company/([A-Za-z0-9_-]+)'
    domain  = urlparse(website).netloc.lstrip("www.") if website else ""
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
                # Remove duplicates, keep order
                seen, unique = set(), []
                for s in slugs:
                    if s not in seen:
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
    # Co-mention query: forces domain + linkedin.com/company to appear together
    try:
        result = search_web(
            query=f'{domain} linkedin.com/company',
            num_results=5,
            summary_question=f"What is the LinkedIn company page URL for {company_name} ({domain})?",
        )
        li_urls = [
            r.get("url", "").split("?")[0].rstrip("/")
            for r in result.get("results", [])
            if "linkedin.com/company/" in r.get("url", "")
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

def get_firmographics(company_name: str, client: anthropic.Anthropic) -> Dict[str, str]:
    empty = {
        "Employee Count": "", "Founded Year": "", "Last Funding Stage": "",
        "Total Funding": "", "Est. Revenue": "", "HQ Location": "",
    }
    try:
        result = search_web(
            query=f"{company_name} employees headcount funding stage total funding founded year headquarters revenue",
            num_results=5,
            summary_question=(
                f"What is {company_name}'s employee count, year founded, last funding stage, "
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

    prompt = f"""Extract firmographic data for "{company_name}" from this research.

Research:
{chr(10).join(snippets[:15])}

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
    domain = urlparse(website).netloc.lstrip("www.") if website else ""
    # Anchor search to the domain to avoid generic name collisions
    query = (
        f'"{domain}" funding launch announcement event customer 2024 2025'
        if domain else
        f'"{company_name}" funding launch announcement event customer 2024 2025'
    )
    try:
        result = search_web(
            query=query,
            num_results=5,
            summary_question=f"What are the most recent notable news items about {company_name} ({domain})? Events, funding, customers, launches.",
        )
    except Exception as e:
        print(f"    News search failed: {e}")
        return ""

    candidates = []
    for r in result.get("results", []):
        url   = r.get("url", "")
        title = r.get("title", "")
        snip  = r.get("snippet") or r.get("summary", "")
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
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Step 6: Founders
# ---------------------------------------------------------------------------

def _find_linkedin_in_url(founder_name: str, company_name: str) -> str:
    """Co-mention query to reliably surface a founder's linkedin.com/in/ URL."""
    try:
        result = search_web(
            query=f'"{founder_name}" "{company_name}" linkedin.com/in',
            num_results=5,
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
    domain = urlparse(website).netloc.lstrip("www.") if website else ""
    name_query = f'"{company_name}" {domain}' if domain else company_name

    # ── Step 1: Identify founder names ───────────────────────────────────────
    try:
        result = search_web(
            query=f"{name_query} founder CEO co-founder",
            num_results=5,
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
{chr(10).join(snippets[:12])}

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
                print(f"      → (not found)")

    # ── Step 3: Twitter — dedicated search for any founder still missing it ───
    for f in founders:
        if f.get("twitter"):
            f["twitter"] = f["twitter"].replace("x.com/", "twitter.com/")
        if f.get("name") and not f.get("twitter"):
            try:
                tw_res = search_web(
                    query=f'"{f["name"]}" {company_name} Twitter',
                    num_results=5,
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
                    posts.append(f"[LinkedIn] {txt[:300]}")
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
                    posts.append(f"[Twitter] {txt[:300]}")
        except Exception as e:
            print(f"    Twitter failed for {founder_name}: {e}")

    if not posts:
        return ""

    try:
        resp = client.messages.create(
            model=CLAUDE_MODEL, max_tokens=200,
            messages=[{"role": "user", "content": f"""Describe {founder_name}'s content strategy in 1-2 sentences based on these posts.

Posts:
{"---".join(posts[:20])}

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

    for key, text in page_texts.items():
        kl = key.lower()
        if any(kw in kl for kw in ["product", "platform", "feature", "solution"]):
            product_t = text[:2500]
        elif any(kw in kl for kw in ["pricing", "plan"]):
            pricing_t = text[:2000]
        elif any(kw in kl for kw in ["customer", "case", "stor", "client", "logo"]):
            customers_t = text[:1500]
        elif any(kw in kl for kw in ["blog", "resource", "insight", "article"]):
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

def get_customer_reviews(
    company_name: str,
    website: str,
    client: anthropic.Anthropic,
) -> str:
    # Find G2 URL
    g2_url = ""
    try:
        result = search_web(
            query=f"{company_name} reviews site:g2.com",
            num_results=3,
            summary_question=f"What is the G2 review page URL for {company_name}?",
        )
        for r in result.get("results", []):
            url = r.get("url", "")
            if "g2.com/products/" in url:
                g2_url = url.split("?")[0]
                break
    except Exception:
        pass

    # Trustpilot fallback
    trustpilot_url = ""
    if not g2_url and website:
        domain = urlparse(website).netloc.lstrip("www.")
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

def get_deal_size(company_name: str, client: anthropic.Anthropic) -> str:
    try:
        result = search_web(
            query=f"{company_name} average deal size ACV annual contract value pricing enterprise mid-market",
            num_results=5,
            summary_question=f"What is the average deal size or ACV for {company_name}?",
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
            messages=[{"role": "user", "content": f"""Find the average deal size or ACV for {company_name}.

Research:
{chr(10).join(snippets[:10])}

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

    prompt = f"""You are a competitive intelligence analyst. Analyze {company_name} from the perspective of a competing product.

Our product context (ICP / positioning):
{icp_context}

{company_name} profile:
{profile_block}

Return JSON with:
- "Competitor Score": score out of 5 as a string (e.g. "3.5", "4.0"). Base it on funding, market traction, product depth, and GTM execution relative to our product.
- "Strength": MAX 2 short lines (~30 words total) on their key competitive advantages. Punchy, specific, grounded in the data above. No preamble, no hedging. If genuinely no signal, write "insufficient data".
- "Weakness": MAX 2 short lines (~30 words total) on their key gaps relative to our positioning. Punchy, specific. If genuinely no signal, write "insufficient data".
- "Target ICP": who they sell to. Use one or more of these exact categories separated by " + ": "SMB", "Mid-Market", "Enterprise", "Early-Stage Startups", "All". E.g. "Mid-Market + Enterprise".

Return only valid JSON."""

    try:
        resp = client.messages.create(
            model=CLAUDE_MODEL, max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        return json.loads(_strip_json_fence(resp.content[0].text))
    except Exception as e:
        print(f"    Final analysis failed: {e}")
        return {"Competitor Score": "", "Strength": "", "Weakness": "", "Target ICP": ""}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Competitor Analysis — deep competitive intelligence")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--sheet-id",             default=None, help="Google Sheet ID")
    src.add_argument("--input-csv",            default=None, help="Read competitors from a CSV file")
    parser.add_argument("--sheet-name",        default="Bird Eye", help="(Sheets only) tab name")
    parser.add_argument("--output-csv",        default=None,
                        help="(CSV only) Where to write output. Defaults to overwriting --input-csv.")
    parser.add_argument("--only",              default="", help="Only process this competitor name (exact match)")
    parser.add_argument("--skip-reviews",      action="store_true", help="Skip G2/Trustpilot scraping")
    parser.add_argument("--skip-twitter",      action="store_true", help="Skip Twitter scraping (saves Apify credits)")
    parser.add_argument("--skip-founder-posts",action="store_true", help="Skip all founder post scraping")
    parser.add_argument("--skip-analysis",     action="store_true", help="Skip final scoring and analysis")
    parser.add_argument("--auto",              action="store_true",
                        help="Run non-interactively. Errors out if context.md is missing required sections.")
    args = parser.parse_args()

    # Pre-step: warn (or abort under --auto) if context.md is too sparse for the analysis to be meaningful.
    check_context_complete(auto=args.auto)

    client = anthropic.Anthropic()

    print("\nLoading ICP context from context/...")
    icp_context = load_icp()

    if args.sheet_id:
        backend = GoogleSheetsBackend(args.sheet_id, args.sheet_name)
        source_label = f"sheet tab '{args.sheet_name}'"
    else:
        backend = CsvBackend(args.input_csv, args.output_csv)
        source_label = f"CSV '{args.input_csv}'"

    print(f"Reading {source_label}...")
    rows = backend.read_all()
    if not rows or len(rows) < 2:
        print("Source empty or no data rows. Exiting.")
        return

    headers:   List[str]       = list(rows[0])
    data_rows: List[List[str]] = rows[1:]
    print(f"Found {len(data_rows)} competitors.")

    name_col = find_col(headers, "Company Name", "Name")
    url_col  = find_col(headers, "Company URL", "URL", "Website")
    if name_col is None:
        print(f"ERROR: No name column found. Headers: {headers}")
        return

    # Ensure all output columns exist, write headers once
    col_map: Dict[str, int] = {}
    for col_name in OUTPUT_COLUMNS:
        idx = ensure_col(headers, col_name)
        col_map[col_name] = idx
        backend.write_header(idx, col_name)

    # ---------------------------------------------------------------------------
    for i, row in enumerate(data_rows):
        competitor = cell(row, name_col)
        if not competitor:
            continue
        if args.only and competitor.lower() != args.only.lower():
            continue

        website = cell(row, url_col) if url_col is not None else ""
        row_num = i + 2

        print(f"\n{'='*60}")
        print(f"[{i+1}/{len(data_rows)}] {competitor}  ({website or 'no URL'})")
        print(f"{'='*60}")

        # Track values written this run so the analysis step can use them
        written: Dict[str, str] = {}

        def get_col(name: str) -> str:
            """Read from original row OR from this run's writes."""
            return written.get(name) or (
                row[col_map[name]].strip()
                if col_map.get(name) is not None and col_map[name] < len(row)
                else ""
            )

        def write_col(name: str, value: str) -> None:
            if value:
                idx = col_map[name]
                backend.write_cell(row_num, idx, value)
                written[name] = value

        # ── Step 1: Website scrape ────────────────────────────────────────────
        scraped: dict = {}
        if website:
            print("  [1] Scraping website...")
            scraped = scrape_website(website)
            pages = len(scraped.get("full_text_by_page", {}))
            print(f"    → {pages} pages scraped")
        else:
            print("  [1] No website URL — skipping scrape")

        # ── Steps 2–10: independent enrichment ────────────────────────────────
        # These depend only on the company name / website / scraped pages, not on
        # each other, and use only Exa + Claude (plus the reviews actor — a single
        # call to a different actor than the founder-post step). Compute them
        # concurrently, then write sequentially so the backend stays single-threaded.
        firm_cols = ["Employee Count", "Founded Year", "Last Funding Stage", "Total Funding", "Est. Revenue", "HQ Location"]
        f1_cols = ["Founder (1) Name", "Founder (1) LinkedIn", "Founder (1) Twitter"]
        f2_cols = ["Founder (2) Name", "Founder (2) LinkedIn", "Founder (2) Twitter"]
        product_cols = [
            "Target Persona (User)", "Sales Motion", "Primary CTA", "Pricing",
            "Customer Stories", "Product Features", "Top Logos", "Marketing Messaging", "SEO",
        ]
        firm_missing     = [c for c in firm_cols if not get_col(c)]
        missing_founders = [c for c in f1_cols + f2_cols if not get_col(c)]
        product_missing  = [c for c in product_cols if not get_col(c)] if scraped else []

        tasks: Dict[str, "callable"] = {}
        if not get_col("Company LinkedIn URL"):
            tasks["li_url"] = lambda: find_linkedin_url(competitor, website, scraped, client)
        if not get_col("Company Description") and scraped:
            tasks["desc"] = lambda: draft_description(competitor, scraped, client)
        if firm_missing:
            tasks["firm"] = lambda: get_firmographics(competitor, client)
        if not get_col("Recent News"):
            tasks["news"] = lambda: get_recent_news(competitor, website, client)
        if missing_founders:
            tasks["founders"] = lambda: find_founders(competitor, website, client)
        if product_missing:
            tasks["prod"] = lambda: extract_product_info(competitor, scraped, client)
        if not get_col("Deal Size"):
            tasks["deal"] = lambda: get_deal_size(competitor, client)
        if not args.skip_reviews and not get_col("Customer Reviews"):
            tasks["reviews"] = lambda: get_customer_reviews(competitor, website, client)

        if tasks:
            print(f"  [2-10] Enriching — {len(tasks)} lookups in parallel: {', '.join(tasks)}")
            res = _run_parallel(tasks)
            for key, (_v, err) in res.items():
                if err:
                    print(f"    ! {key} lookup failed: {err}")
        else:
            res = {}
            print("  [2-10] All enrichment columns already filled — skipping")

        def _val(key, default):
            value, _err = res.get(key, (None, None))
            return value if value is not None else default

        # Step 2: Company LinkedIn URL
        if "li_url" in tasks:
            write_col("Company LinkedIn URL", _val("li_url", ""))

        # Step 3: Company description
        if "desc" in tasks:
            write_col("Company Description", _val("desc", ""))

        # Step 4: Firmographics
        if "firm" in tasks:
            firm = _val("firm", {}) or {}
            for c in firm_missing:
                write_col(c, firm.get(c, ""))

        # Step 5: Recent news
        if "news" in tasks:
            write_col("Recent News", _val("news", ""))

        # Step 6: Founders (write, then rebuild the list from final cell values)
        if "founders" in tasks:
            found = _val("founders", []) or []
            f1 = found[0] if len(found) > 0 else {}
            f2 = found[1] if len(found) > 1 else {}
            for col_name, key in [("Founder (1) Name", "name"), ("Founder (1) LinkedIn", "linkedin"), ("Founder (1) Twitter", "twitter")]:
                if not get_col(col_name): write_col(col_name, f1.get(key, ""))
            for col_name, key in [("Founder (2) Name", "name"), ("Founder (2) LinkedIn", "linkedin"), ("Founder (2) Twitter", "twitter")]:
                if not get_col(col_name): write_col(col_name, f2.get(key, ""))
        founders: List[Dict[str, str]] = [
            {"name": get_col("Founder (1) Name"), "linkedin": get_col("Founder (1) LinkedIn"), "twitter": get_col("Founder (1) Twitter")},
            {"name": get_col("Founder (2) Name"), "linkedin": get_col("Founder (2) LinkedIn"), "twitter": get_col("Founder (2) Twitter")},
        ]

        # Step 8: Product info
        if "prod" in tasks:
            prod = _val("prod", {}) or {}
            for c in product_missing:
                write_col(c, prod.get(c, ""))

        # Step 9: Customer reviews
        if "reviews" in tasks:
            review_val = (_val("reviews", "") or "") or "not available"
            idx = col_map["Customer Reviews"]
            backend.write_cell(row_num, idx, review_val)
            written["Customer Reviews"] = review_val
        elif args.skip_reviews:
            print("  [9] Skipping reviews (--skip-reviews)")

        # Step 10: Deal size
        if "deal" in tasks:
            write_col("Deal Size", _val("deal", ""))

        # ── Step 7: Founder post types (throttled actors → kept sequential) ────
        founder_post_summaries: List[str] = []
        if args.skip_founder_posts:
            print("  [7] Skipping founder posts (--skip-founder-posts)")
        else:
            for fi, (founder, post_col) in enumerate(zip(
                founders[:2],
                ["Founder (1) Post type", "Founder (2) Post type"],
            )):
                fname = founder.get("name", "") if founder else ""
                if not fname:
                    continue
                if get_col(post_col):
                    founder_post_summaries.append(f"[{fname}] {get_col(post_col)}")
                    continue
                print(f"  [7.{fi+1}] Getting post type for {fname}...")
                pt = get_founder_post_type(
                    fname,
                    founder.get("linkedin", ""),
                    founder.get("twitter", ""),
                    client,
                    skip_twitter=args.skip_twitter,
                )
                write_col(post_col, pt)
                if pt:
                    founder_post_summaries.append(f"[{fname}] {pt}")
                print(f"    → {pt[:100] or '(no data)'}")
                time.sleep(2)

        # ── Step 11: Content type ─────────────────────────────────────────────
        if not get_col("Content Type"):
            print("  [11] Synthesising content type...")
            ct = get_content_type(competitor, founder_post_summaries, scraped, client)
            write_col("Content Type", ct)
            print(f"    → {ct[:100] or '(none)'}")
        else:
            print("  [11] Content Type already filled — skipping")

        # ── Step 12: Final analysis ───────────────────────────────────────────
        if args.skip_analysis:
            print("  [12] Skipping analysis (--skip-analysis)")
        else:
            analysis_cols = ["Competitor Score", "Strength", "Weakness", "Target ICP"]
            missing_analysis = [c for c in analysis_cols if not get_col(c)]
            if missing_analysis:
                print("  [12] Running final analysis...")
                profile = {c: get_col(c) for c in OUTPUT_COLUMNS if get_col(c)}
                analysis = analyze_competitor(competitor, profile, icp_context, client)
                for c in missing_analysis:
                    write_col(c, analysis.get(c, ""))
                print(f"    → Score: {analysis.get('Competitor Score','?')} | ICP: {analysis.get('Target ICP','?')}")
                print(f"    → Strength:  {analysis.get('Strength','')[:80]}")
                print(f"    → Weakness:  {analysis.get('Weakness','')[:80]}")
            else:
                print("  [12] Analysis columns already filled — skipping")

    print(f"\n{'='*60}")
    print(f"Done. Processed {len([r for r in data_rows if cell(r, name_col)])} competitors.")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
