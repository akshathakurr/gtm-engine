"""
Email Outreach Workflow — Company Qualification + Enrichment + Prioritization + Email Outreach Prep

The canonical input is the **company** (name, optionally URL/LinkedIn). Person
info is optional and added later — only for companies that qualify (P0 by
default), since finding buyers, scraping posts, and writing copy is expensive.

Flow:
  1  — Enrich every company: URL, LinkedIn, one-liner description, employees,
       est revenue, founded year, total funding, HQ, 2-3 competitors
  2  — Score every company: ICP Segment + Priority (P0/P1/P2) + 1-line reasoning
  3  — Filter to outreach batch (P0 only by default; --include-p1 / --include-p2)
  4  — Find the buyer at each P0 company (name, position, LinkedIn) via web search
  5  — Classify buyer persona: Decision Maker / Champion / Non Decision Maker
  6  — Find email via Apollo Contact Finder
  7  — Small talk personalisation (Small Talk Scraper)
  8  — Scrape + filter LinkedIn posts by ICP relevance criteria
  9  — Generate personalisation hooks (Personalisation Hook Skill)
  10 — Write personalised email copy (Email Copy Writer Skill)

Usage:
  python -m workflows.email_outreach.workflow --sheet-id SHEET_ID
  python -m workflows.email_outreach.workflow --input-csv companies.csv --output-csv companies.out.csv
  python -m workflows.email_outreach.workflow --sheet-id SHEET_ID --include-p1
  python -m workflows.email_outreach.workflow --sheet-id SHEET_ID --limit 5   # quick test run
  python -m workflows.email_outreach.workflow --sheet-id SHEET_ID --enrich-fields employee_count,total_funding,hq
"""

import os
import re
import csv
import json
import time
import argparse
from typing import Optional, List, Dict

import anthropic

from config import CLAUDE_MODEL
from workflows._common import (
    strip_json_fence as _strip_json_fence,
    gws_read_sheet, gws_write_range, col_letter, ensure_col, cell, load_icp,
)
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


# Snake_case keys are canonical; `label` is the default header name when the
# sheet has no semantic match. Add or remove an enrichment field in one place.
ENRICH_FIELDS: List[Dict[str, str]] = [
    {"key": "company_url",         "label": "Company URL",          "desc": "Company website URL (homepage)"},
    {"key": "company_linkedin",    "label": "Company LinkedIn URL", "desc": "Company LinkedIn page URL"},
    {"key": "company_description", "label": "Company Description",  "desc": "One-line description of what the company does"},
    {"key": "employee_count",      "label": "Employee Count",       "desc": "Headcount / number of employees"},
    {"key": "est_revenue",         "label": "Est Revenue",          "desc": "Estimated annual revenue"},
    {"key": "founded_year",        "label": "Founded Year",         "desc": "Year company was founded"},
    {"key": "total_funding",       "label": "Total Funding",        "desc": "Total funding raised"},
    {"key": "hq",                  "label": "HQ",                   "desc": "HQ city"},
    {"key": "competitors",         "label": "Competitors",          "desc": "2-3 immediate direct competitors, comma-separated"},
]

DEFAULT_MAX_POSTS = 15
DEFAULT_DAYS_BACK = 90

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

import threading
from concurrent.futures import ThreadPoolExecutor


class _RateLimiter:
    """Spaces successive acquire() calls >= min_interval apart (thread-safe).

    Spacing applies to call *starts*, so slow work after acquire() overlaps
    across threads without bursting a rate-limited service.
    """

    def __init__(self, min_interval: float):
        self._min_interval = min_interval
        self._lock = threading.Lock()
        self._next = 0.0

    def acquire(self) -> None:
        if self._min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._next - now
            if wait > 0:
                time.sleep(wait)
                now += wait
            self._next = now + self._min_interval


def _map_rate_limited(fn, items: list, *, min_interval: float = 0.0, max_workers: int = ENRICH_CONCURRENCY):
    """Run fn(item) over a bounded pool, starts spaced >= min_interval.

    Returns (results, errors) aligned to `items`. fn exceptions are captured
    (result None, error set), never raised, so one bad item can't kill the batch.
    Results preserve input order.
    """
    n = len(items)
    if n == 0:
        return [], []
    limiter = _RateLimiter(min_interval)
    results: List[Optional[object]] = [None] * n
    errors: List[Optional[Exception]] = [None] * n

    def task(idx, item):
        limiter.acquire()
        try:
            return idx, fn(item), None
        except Exception as e:  # noqa: BLE001 — captured per item, surfaced to caller
            return idx, None, e

    with ThreadPoolExecutor(max_workers=min(max_workers, n)) as ex:
        for fut in [ex.submit(task, i, it) for i, it in enumerate(items)]:
            idx, res, err = fut.result()
            results[idx] = res
            errors[idx] = err
    return results, errors


# ---------------------------------------------------------------------------
# Google Sheets helpers
# ---------------------------------------------------------------------------

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
        ltr = col_letter(col_idx)
        gws_write_range(self.sheet_id, f"{self.sheet_name}!{ltr}1", [[name]])

    def write_cell(self, row_num: int, col_idx: int, value: str) -> None:
        ltr = col_letter(col_idx)
        gws_write_range(self.sheet_id, f"{self.sheet_name}!{ltr}{row_num}", [[value]])

    def write_column(self, col_idx: int, values: List[str]) -> None:
        if not values:
            return
        ltr = col_letter(col_idx)
        rng = f"{self.sheet_name}!{ltr}2:{ltr}{len(values) + 1}"
        gws_write_range(self.sheet_id, rng, [[v] for v in values])


class CsvBackend:
    """In-memory rows; rewrites the output CSV after every write so partial progress survives crashes."""

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

    def write_column(self, col_idx: int, values: List[str]) -> None:
        for i, v in enumerate(values):
            idx = i + 1
            self._ensure(idx, col_idx)
            self.rows[idx][col_idx] = v
        self._flush()


# ---------------------------------------------------------------------------
# Sheet column utilities
# ---------------------------------------------------------------------------

def cell_combined(row: List[str], indices: List[int]) -> str:
    parts = [row[i].strip() for i in indices if i < len(row) and row[i] and row[i].strip()]
    return " ".join(parts)


def parse_post_config(icp_context: str) -> Dict:
    max_posts = DEFAULT_MAX_POSTS
    days_back = DEFAULT_DAYS_BACK
    for line in icp_context.splitlines():
        ll = line.lower()
        if "max posts per profile" in ll:
            m = re.search(r"\d+", line)
            if m:
                max_posts = int(m.group())
        elif "days back" in ll:
            m = re.search(r"\d+", line)
            if m:
                days_back = int(m.group())
    return {"max_posts": max_posts, "days_back": days_back}


def detect_columns(
    headers: List[str],
    sample_row: List[str],
    required_fields: Dict[str, str],
    client: anthropic.Anthropic,
) -> Dict[str, List[int]]:
    padded = list(sample_row) + [""] * max(0, len(headers) - len(sample_row))
    sample_block = "\n".join(
        f"  col {i} ({h}): {(padded[i] or '')[:80]}"
        for i, h in enumerate(headers)
    )
    fields_block = "\n".join(f"- {name}: {desc}" for name, desc in required_fields.items())

    prompt = f"""Map spreadsheet columns to standard workflow fields.

Sheet columns (index: header — sample value):
{sample_block}

Required fields:
{fields_block}

For each required field, return the column INDEX(es) whose contents best match.
- If a field spans multiple columns (e.g., name split into firstName + lastName), return all relevant indices.
- Headers don't have to match field names exactly — judge by content. Use the sample to disambiguate.
- Return null for fields that have no matching column.

Return JSON: {{"<field_name>": [<idx>, ...] or null, ...}}
Only valid JSON, no explanation."""

    resp = client.messages.create(
        model=CLAUDE_MODEL, temperature=0, max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    parsed = json.loads(_strip_json_fence(resp.content[0].text))
    return {
        k: (v if isinstance(v, list) else [v] if v is not None else [])
        for k, v in parsed.items()
    }


def get_or_create_col(headers: List[str], mapping: Dict[str, List[int]], field_key: str, default_name: str) -> int:
    # Guard against the LLM mapping a field to an out-of-bounds index — fall through
    # to the default column path so we don't silently write into a phantom column.
    indices = [i for i in (mapping.get(field_key) or []) if 0 <= i < len(headers)]
    if indices:
        return indices[0]
    return ensure_col(headers, default_name)


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
    chunk_results, _ = _map_rate_limited(_score_chunk, chunks, max_workers=ENRICH_CONCURRENCY)
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
    chunk_results, _ = _map_rate_limited(_classify_chunk, chunks, max_workers=ENRICH_CONCURRENCY)
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
        return result.get("hooks", "")
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _apply_enrichment(lead: Dict, enriched: Dict[str, str], fields: List[Dict[str, str]]) -> None:
    """Copy only the fields the run actually enriched onto the lead, so a
    restricted --enrich-fields run doesn't blank out untouched columns."""
    for f in fields:
        lead[f["key"]] = enriched.get(f["key"], "")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Email Outreach Workflow — Enrich, Prioritize, Find Buyer + Email, Prep Outreach"
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--sheet-id", default=None,
                     help="Google Sheet ID. Requires gws CLI installed and authed.")
    src.add_argument("--input-csv", default=None,
                     help="Path to a CSV file of companies (alternative to Sheets).")
    parser.add_argument("--sheet-name", default="Sheet1",
                        help="(Sheets only) Sheet tab name. Default: Sheet1")
    parser.add_argument("--output-csv", default=None,
                        help="(CSV only) Where to write output. Defaults to overwriting --input-csv.")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="Process only the first N rows (quick, cheap test runs)")
    parser.add_argument("--add-persona", action="append", default=[], metavar="TITLE",
                        help="One-time: also treat this title as a buyer (can repeat)")
    parser.add_argument("--remove-persona", action="append", default=[], metavar="TITLE",
                        help="One-time: exclude this title from buyer consideration (can repeat)")
    parser.add_argument("--enrich-fields", default=None,
                        help=f"Comma-separated enrichment field keys (default: {', '.join(f['key'] for f in ENRICH_FIELDS)})")
    parser.add_argument("--include-p1", action="store_true",
                        help="Also include P1 leads in outreach batch (default: P0 only)")
    parser.add_argument("--include-p2", action="store_true",
                        help="Also include P2 leads in outreach batch")
    parser.add_argument("--skip-enrich",     action="store_true")
    parser.add_argument("--skip-emails",     action="store_true",
                        help="Skip Apollo email lookup (Step 6)")
    parser.add_argument("--skip-small-talk", action="store_true")
    parser.add_argument("--skip-posts",      action="store_true")
    parser.add_argument("--skip-hooks",      action="store_true")
    parser.add_argument("--skip-copy",       action="store_true")
    args = parser.parse_args()

    client = anthropic.Anthropic()

    if args.skip_enrich:
        enrich_fields: List[Dict[str, str]] = []
    elif args.enrich_fields:
        wanted = {k.strip() for k in args.enrich_fields.split(",")}
        enrich_fields = [f for f in ENRICH_FIELDS if f["key"] in wanted]
    else:
        enrich_fields = list(ENRICH_FIELDS)

    print("\nLoading ICP context from context/...")
    icp_context = load_icp()
    post_config = parse_post_config(icp_context)
    print(f"  Post scraping config: max_posts={post_config['max_posts']}, days_back={post_config['days_back']}")

    if args.sheet_id:
        backend = GoogleSheetsBackend(args.sheet_id, args.sheet_name)
        print(f"Reading sheet {args.sheet_id} tab '{args.sheet_name}'...")
    else:
        backend = CsvBackend(args.input_csv, args.output_csv)
        out = args.output_csv or args.input_csv
        print(f"Reading CSV {args.input_csv} (writing to {out})...")
    rows = backend.read_all()
    if not rows or len(rows) < 2:
        print("Sheet/CSV is empty or has no data rows. Exiting.")
        return

    headers: List[str] = list(rows[0])
    data_rows: List[List[str]] = rows[1:]
    if args.limit is not None:
        data_rows = data_rows[:max(0, args.limit)]
        print(f"--limit {args.limit}: processing first {len(data_rows)} row(s).")
    print(f"Found {len(data_rows)} rows.")

    print("\nDetecting columns via Claude...")
    mapping = detect_columns(
        headers,
        data_rows[0] if data_rows else [],
        {
            # inputs (only company is required)
            "company":  "Current company name. REQUIRED.",
            "name":     "Full person name (combine first + last name columns if split). Optional.",
            "linkedin": "LinkedIn profile URL of the person. Optional.",
            "position": "Job title / role / position. Optional.",
            "email":    "Email address of the person. Optional.",
            # outputs (ok if missing — will be appended)
            "company_url":         "Company website URL (homepage)",
            "company_linkedin":    "Company LinkedIn page URL",
            "company_description": "One-line description of what the company does",
            "employee_count":      "Headcount / number of employees",
            "est_revenue":         "Estimated annual revenue",
            "founded_year":        "Year company was founded",
            "total_funding":       "Total funding raised",
            "hq":                  "HQ city",
            "competitors":         "2-3 immediate direct competitors of the company",
            "icp_segment":         "ICP segment / tier the company belongs to",
            "priority":            "Priority tier: P0 / P1 / P2",
            "reasoning":           "Reasoning explaining the priority",
            "buyer_persona":       "Buyer-persona classification: Decision Maker / Champion / Non DM",
            "post_links":          "LinkedIn post URLs of the lead",
            "small_talk":          "Small-talk / personalisation details",
            "hooks":               "Talking points / personalisation hooks",
            "copy":                "Final email copy / message",
        },
        client,
    )
    print(f"  Mapping: {mapping}")

    if not mapping.get("company"):
        print("ERROR: Could not detect a Company column. Company is the only required input.")
        print(f"Headers: {headers}")
        return

    leads: List[Dict] = []
    for row in data_rows:
        leads.append({
            "company":  cell_combined(row, mapping.get("company", [])),
            "name":     cell_combined(row, mapping.get("name", [])),
            "linkedin": cell_combined(row, mapping.get("linkedin", [])),
            "position": cell_combined(row, mapping.get("position", [])),
            "email":    cell_combined(row, mapping.get("email", [])),
        })

    # ------------------------------------------------------------------
    # Step 1: Enrich every company
    # ------------------------------------------------------------------
    if not enrich_fields:
        print("\n--- Step 1: Skipping enrichment (--skip-enrich) ---")
    else:
        print(f"\n--- Step 1: Enriching {len(leads)} company(ies) ---")
        print(f"  Fields: {', '.join(f['key'] for f in enrich_fields)}")

        col_idx_by_key: Dict[str, int] = {}
        for f in enrich_fields:
            idx = get_or_create_col(headers, mapping, f["key"], f["label"])
            col_idx_by_key[f["key"]] = idx
            backend.write_header(idx, headers[idx])

        company_cache: Dict[str, Dict[str, str]] = {}

        def _all_filled(row) -> bool:
            return all(
                col_idx_by_key[f["key"]] < len(row) and row[col_idx_by_key[f["key"]]].strip()
                for f in enrich_fields
            )

        # Enrich each UNIQUE company that still needs it, concurrently (Claude +
        # Exa). Deduping by name also avoids paying to enrich the same company
        # twice when multiple leads share it.
        to_enrich: List[str] = []
        seen_companies: set = set()
        for i, lead in enumerate(leads):
            company = lead["company"]
            if not company or _all_filled(data_rows[i]) or company in seen_companies:
                continue
            seen_companies.add(company)
            to_enrich.append(company)

        if to_enrich:
            print(f"  Enriching {len(to_enrich)} unique company(ies) in parallel...")
            results, errors = _map_rate_limited(
                lambda c: enrich_company(c, enrich_fields, client),
                to_enrich, max_workers=ENRICH_CONCURRENCY,
            )
            for c, r, e in zip(to_enrich, results, errors):
                if e:
                    print(f"    ! {c} enrichment failed: {e}")
                company_cache[c] = r or {}

        # Apply + write sequentially so the backend is never touched concurrently.
        for i, lead in enumerate(leads):
            company = lead["company"]
            if not company:
                continue
            row     = data_rows[i]
            row_num = i + 2
            if _all_filled(row):
                enriched = {f["key"]: cell(row, col_idx_by_key[f["key"]]) for f in enrich_fields}
            else:
                enriched = company_cache.get(company, {})
                for f in enrich_fields:
                    backend.write_cell(row_num, col_idx_by_key[f["key"]], enriched.get(f["key"], ""))
            _apply_enrichment(leads[i], enriched, enrich_fields)

    # ------------------------------------------------------------------
    # Step 2: Score (ICP Segment + Priority + Reasoning)
    # ------------------------------------------------------------------
    print("\n--- Step 2: Scoring companies (ICP Segment + Priority) ---")
    scores = score_companies(leads, icp_context, client)

    icp_col_idx       = get_or_create_col(headers, mapping, "icp_segment", "ICP Segment")
    priority_col_idx  = get_or_create_col(headers, mapping, "priority",    "Priority")
    reasoning_col_idx = get_or_create_col(headers, mapping, "reasoning",   "Reasoning")
    backend.write_header(icp_col_idx,       headers[icp_col_idx])
    backend.write_header(priority_col_idx,  headers[priority_col_idx])
    backend.write_header(reasoning_col_idx, headers[reasoning_col_idx])
    backend.write_column(icp_col_idx,       [s.get("icp_segment", "") for s in scores])
    backend.write_column(priority_col_idx,  [s["priority"] for s in scores])
    backend.write_column(reasoning_col_idx, [s["reasoning"] for s in scores])

    p0 = sum(1 for s in scores if s["priority"] == "P0")
    p1 = sum(1 for s in scores if s["priority"] == "P1")
    p2 = sum(1 for s in scores if s["priority"] == "P2")
    print(f"  P0: {p0} | P1: {p1} | P2: {p2}")

    # ------------------------------------------------------------------
    # Step 3: Filter outreach batch (P0 only by default)
    # ------------------------------------------------------------------
    print("\n--- Step 3: Selecting outreach batch ---")
    tiers = {"P0"}
    if args.include_p1:
        tiers.add("P1")
    if args.include_p2:
        tiers.add("P2")

    outreach_indices = [i for i, s in enumerate(scores) if s["priority"] in tiers]
    print(f"  Tiers in batch: {sorted(tiers)} → {len(outreach_indices)} leads")
    if not outreach_indices:
        print("  No outreach leads selected. Exiting.")
        return

    # Step-spanning caches
    post_data_by_lead:  Dict[int, List[Dict]] = {}
    small_talk_by_lead: Dict[int, str]        = {}
    hooks_by_lead:      Dict[int, str]        = {}
    classifications:    Dict[int, str]        = {}  # filled in Step 5
    email_by_lead:      Dict[int, str]        = {}  # filled in Step 6

    # ------------------------------------------------------------------
    # Step 4: Find buyer at each P0 company
    # ------------------------------------------------------------------
    print(f"\n--- Step 4: Finding buyers for {len(outreach_indices)} leads ---")

    name_col_idx     = get_or_create_col(headers, mapping, "name",     "Name")
    position_col_idx = get_or_create_col(headers, mapping, "position", "Position")
    linkedin_col_idx = get_or_create_col(headers, mapping, "linkedin", "LinkedIn Profile")
    backend.write_header(name_col_idx,     headers[name_col_idx])
    backend.write_header(position_col_idx, headers[position_col_idx])
    backend.write_header(linkedin_col_idx, headers[linkedin_col_idx])

    # Each lead's buyer lookup is independent Claude + Exa work — compute in
    # parallel, then write sequentially.
    def _buyer_task(i: int):
        lead = leads[i]
        if (lead.get("name") or "").strip():
            if not (lead.get("linkedin") or "").strip():
                return ("linkedin", find_linkedin_url(lead["name"], lead["company"], client))
            return ("skip", None)
        return ("buyer", find_buyer_at_company(lead["company"], icp_context, client))

    buyer_results, buyer_errors = _map_rate_limited(
        _buyer_task, outreach_indices, max_workers=ENRICH_CONCURRENCY,
    )
    for i, r, e in zip(outreach_indices, buyer_results, buyer_errors):
        lead    = leads[i]
        row_num = i + 2
        if e:
            print(f"  {lead['company']} — buyer lookup failed: {e}")
            continue
        kind, val = r
        if kind == "skip":
            print(f"  {lead['name']} ({lead['company']}) — already filled, skipping")
        elif kind == "linkedin":
            if val:
                lead["linkedin"] = val
                backend.write_cell(row_num, linkedin_col_idx, val)
                print(f"  {lead['name']} ({lead['company']}) → {val}")
        else:  # buyer
            buyer = val or {}
            if buyer.get("name"):
                lead["name"]     = buyer["name"]
                lead["position"] = buyer.get("position", "")
                lead["linkedin"] = buyer.get("linkedin", "")
                backend.write_cell(row_num, name_col_idx,     lead["name"])
                backend.write_cell(row_num, position_col_idx, lead["position"])
                if lead["linkedin"]:
                    backend.write_cell(row_num, linkedin_col_idx, lead["linkedin"])
                print(f"  {lead['company']} → {lead['name']} ({lead['position']})")
            else:
                print(f"  {lead['company']} → could not find a buyer")

    # ------------------------------------------------------------------
    # Step 5: Classify buyer persona — only for P0 with a buyer
    # ------------------------------------------------------------------
    print("\n--- Step 5: Classifying buyer personas ---")
    if args.add_persona:
        print(f"  + Adding for this run: {args.add_persona}")
    if args.remove_persona:
        print(f"  - Removing for this run: {args.remove_persona}")

    persona_inputs = [(i, leads[i]) for i in outreach_indices if (leads[i].get("name") or "").strip()]
    if persona_inputs:
        sub_leads = [l for _, l in persona_inputs]
        sub_cls   = classify_personas(
            sub_leads, icp_context, args.add_persona, args.remove_persona, client,
        )
        buyer_col_idx = get_or_create_col(headers, mapping, "buyer_persona", "Buyer Persona Match")
        backend.write_header(buyer_col_idx, headers[buyer_col_idx])
        for (i, _), c in zip(persona_inputs, sub_cls):
            classifications[i] = c
            backend.write_cell(i + 2, buyer_col_idx, c)

        dm_count    = sum(1 for c in classifications.values() if c == "Decision Maker")
        champ_count = sum(1 for c in classifications.values() if c == "Champion")
        ndm_count   = sum(1 for c in classifications.values() if c == "Non Decision Maker")
        print(f"  DMs: {dm_count} | Champions: {champ_count} | Non DMs: {ndm_count}")
    else:
        print("  No P0 leads with a buyer to classify — skipping.")
        dm_count = champ_count = ndm_count = 0

    # ------------------------------------------------------------------
    # Step 6: Find emails via Apollo
    # ------------------------------------------------------------------
    if args.skip_emails:
        print("\n--- Step 6: Skipping email lookup (--skip-emails) ---")
    else:
        print(f"\n--- Step 6: Finding emails for {len(outreach_indices)} leads ---")

        email_col_idx = get_or_create_col(headers, mapping, "email", "Email")
        backend.write_header(email_col_idx, headers[email_col_idx])

        # Decide who still needs a lookup; reuse anything already present.
        need_email: List[int] = []
        for i in outreach_indices:
            lead = leads[i]
            row  = data_rows[i]
            if not (lead.get("name") or "").strip():
                print(f"  {lead['company']} — no buyer, skipping email lookup")
                continue
            existing = cell(row, email_col_idx) if email_col_idx < len(row) else ""
            if existing or (lead.get("email") or "").strip():
                email_by_lead[i] = existing or lead["email"]
                continue
            need_email.append(i)

        # Apollo is rate-limited — space lookups APOLLO_MIN_INTERVAL apart, but
        # overlap their latency across a small pool.
        results, errors = _map_rate_limited(
            lambda i: find_email(leads[i]), need_email,
            min_interval=APOLLO_MIN_INTERVAL, max_workers=APOLLO_CONCURRENCY,
        )
        for i, r, e in zip(need_email, results, errors):
            email = "" if e else (r or "")
            if e:
                print(f"  {leads[i]['name']} — email lookup failed: {e}")
            email_by_lead[i] = email
            leads[i]["email"] = email
            backend.write_cell(i + 2, email_col_idx, email)
            print(f"  {leads[i]['name']} ({leads[i]['company']}) → {email or '(not found)'}")

    # ------------------------------------------------------------------
    # Step 7: Small talk
    # ------------------------------------------------------------------
    if args.skip_small_talk:
        print("\n--- Step 7: Skipping small talk (--skip-small-talk) ---")
    elif not _SMALL_TALK_AVAILABLE:
        print("\n--- Step 7: Small Talk Scraper failed to import — skipping ---")
    else:
        print(f"\n--- Step 7: Gathering small talk for {len(outreach_indices)} leads ---")

        small_talk_col_idx = get_or_create_col(headers, mapping, "small_talk", "Small Talk")
        backend.write_header(small_talk_col_idx, headers[small_talk_col_idx])

        st_indices = [i for i in outreach_indices if (leads[i].get("name") or "").strip()]
        results, errors = _map_rate_limited(
            lambda i: scrape_small_talk(
                profile_url=leads[i].get("linkedin", ""), name=leads[i]["name"], company=leads[i]["company"],
            ),
            st_indices, max_workers=ENRICH_CONCURRENCY,
        )
        for i, r, e in zip(st_indices, results, errors):
            detail = "" if e else (r or "")
            if e:
                print(f"  {leads[i]['name']} — small talk failed: {e}")
            small_talk_by_lead[i] = detail
            backend.write_cell(i + 2, small_talk_col_idx, detail)
            print(f"  {leads[i]['name']} ({leads[i]['company']}) → {(detail[:80] + '...') if len(detail) > 80 else (detail or '(none)')}")

    # ------------------------------------------------------------------
    # Step 8: Posts (LinkedIn) — needs lead.linkedin
    # ------------------------------------------------------------------
    if args.skip_posts:
        print("\n--- Step 8: Skipping post scraping (--skip-posts) ---")
    else:
        has_linkedin = [i for i in outreach_indices if (leads[i].get("linkedin") or "").strip()]
        print(f"\n--- Step 8: Scraping LinkedIn posts for {len(has_linkedin)} leads ---")
        print(f"  Config: {post_config['max_posts']} posts / {post_config['days_back']} days")

        post_links_col_idx = get_or_create_col(headers, mapping, "post_links", "LinkedIn Post Links")
        backend.write_header(post_links_col_idx, headers[post_links_col_idx])

        # profile-posts actor throttles on bursts — space starts POSTS_MIN_INTERVAL
        # apart, overlap run-times across a small pool.
        results, errors = _map_rate_limited(
            lambda i: scrape_and_filter_posts(
                profile_url=leads[i]["linkedin"], icp_context=icp_context,
                max_posts=post_config["max_posts"], days_back=post_config["days_back"], client=client,
            ),
            has_linkedin, min_interval=POSTS_MIN_INTERVAL, max_workers=POSTS_CONCURRENCY,
        )
        for i, r, e in zip(has_linkedin, results, errors):
            if e:
                print(f"  {leads[i]['name']} — post scraping failed: {e}")
                post_data_by_lead[i] = []
                continue
            post_data_by_lead[i] = r["posts_data"]
            cell_value = "\n".join(r["urls"]) if r["urls"] else ""
            backend.write_cell(i + 2, post_links_col_idx, cell_value)
            print(f"  {leads[i]['name']} ({leads[i]['company']}) → {len(r['urls'])} post(s) matched")

    # ------------------------------------------------------------------
    # Step 9: Personalisation hooks
    # ------------------------------------------------------------------
    if args.skip_hooks:
        print("\n--- Step 9: Skipping personalisation hooks (--skip-hooks) ---")
    elif not _PERSONALISATION_AVAILABLE:
        print("\n--- Step 9: Personalisation Hook Skill failed to import — skipping ---")
    else:
        print(f"\n--- Step 9: Generating personalisation hooks for {len(outreach_indices)} leads ---")

        hooks_col_idx = get_or_create_col(headers, mapping, "hooks", "Personalisation Hook")
        backend.write_header(hooks_col_idx, headers[hooks_col_idx])

        hook_indices = [i for i in outreach_indices if (leads[i].get("name") or "").strip()]

        def _hook_task(i: int) -> str:
            lead = leads[i]
            return generate_personalisation_hooks(
                name=lead["name"], company=lead["company"], position=lead["position"],
                matching_posts=post_data_by_lead.get(i, []),
                small_talk=small_talk_by_lead.get(i, ""),
                icp_context=icp_context,
                competitors=lead.get("competitors", ""),
                company_description=lead.get("company_description", ""),
                employee_count=lead.get("employee_count", ""),
                est_revenue=lead.get("est_revenue", ""),
                total_funding=lead.get("total_funding", ""),
                hq=lead.get("hq", ""),
            )

        results, errors = _map_rate_limited(_hook_task, hook_indices, max_workers=ENRICH_CONCURRENCY)
        for i, r, e in zip(hook_indices, results, errors):
            hooks = "" if e else (r or "")
            if e:
                print(f"  {leads[i]['name']} — hook generation failed: {e}")
            hooks_by_lead[i] = hooks
            backend.write_cell(i + 2, hooks_col_idx, hooks)
            print(f"  {leads[i]['name']} ({leads[i]['company']}) → {(hooks[:100] + '...') if len(hooks) > 100 else (hooks or '(none)')}")

    # ------------------------------------------------------------------
    # Step 10: Email copy
    # ------------------------------------------------------------------
    if args.skip_copy:
        print("\n--- Step 10: Skipping email copy (--skip-copy) ---")
    elif not _EMAIL_COPY_AVAILABLE:
        print("\n--- Step 10: Email Copy Writer Skill failed to import — skipping ---")
    else:
        print(f"\n--- Step 10: Writing email copy for {len(outreach_indices)} leads ---")

        copy_col_idx = get_or_create_col(headers, mapping, "copy", "Email Copy")
        backend.write_header(copy_col_idx, headers[copy_col_idx])

        copy_indices = [i for i in outreach_indices if (leads[i].get("name") or "").strip()]

        def _copy_task(i: int) -> str:
            lead = leads[i]
            return write_email_copy(
                name=lead["name"], company=lead["company"], position=lead["position"],
                email=email_by_lead.get(i, lead.get("email", "")),
                buyer_persona=classifications.get(i, ""),
                priority=scores[i]["priority"],
                matching_posts=post_data_by_lead.get(i, []),
                small_talk=small_talk_by_lead.get(i, ""),
                personalisation_hook=hooks_by_lead.get(i, ""),
                icp_context=icp_context,
                employee_count=lead.get("employee_count", ""),
                est_revenue=lead.get("est_revenue", ""),
                total_funding=lead.get("total_funding", ""),
                hq=lead.get("hq", ""),
                competitors=lead.get("competitors", ""),
            )

        results, errors = _map_rate_limited(_copy_task, copy_indices, max_workers=ENRICH_CONCURRENCY)
        for i, r, e in zip(copy_indices, results, errors):
            copy = "" if e else (r or "")
            if e:
                print(f"  {leads[i]['name']} — copy generation failed: {e}")
            backend.write_cell(i + 2, copy_col_idx, copy)
            print(f"  {leads[i]['name']} ({leads[i]['company']}) → {(copy[:100] + '...') if len(copy) > 100 else (copy or '(none)')}")

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    print("\n======= Done =======")
    print(f"  Companies processed:  {len(leads)}")
    print(f"  Priority scores:      {p0} P0 | {p1} P1 | {p2} P2")
    print(f"  Outreach batch:       {len(outreach_indices)} leads")
    print(f"  Buyer personas (P0):  {dm_count} DMs | {champ_count} Champions | {ndm_count} Non DMs")


if __name__ == "__main__":
    main()
