"""
LinkedIn Outreach Workflow — Buyer Qualification + Enrichment + Prioritization + Outreach Prep

Steps:
  1  — Classify each lead: Decision Maker / Champion / Non Decision Maker
  2  — Enrich Decision Maker companies with firmographic data via Web Search
  3  — Score all leads P0 / P1 / P2 with 1-2 line reasoning
  4  — Select outreach batch: all P0; if P0 < 100 also include P1 until we reach 100
  5  — Find 3-4 direct competitors for each filtered lead's company (Web Search)
  6  — Scrape LinkedIn posts for each filtered lead; filter by ICP criteria; write post URLs
  7  — Gather small talk details for each filtered lead (Small Talk Scraper)
  8  — Generate personalisation talking points (Personalisation Hook Skill)
  9  — Write personalised LinkedIn copy (LinkedIn Copy Writer Skill)

Buyer criteria, ICP scoring, and post filtering criteria come from Context/{project}/icp.md.
Post scraping config (max_posts, days_back) is also read from that file — with scraper defaults
as fallback if the fields are not filled in.

Usage:
  python -m workflows.linkedin_outreach.workflow --sheet-id SHEET_ID
  python -m workflows.linkedin_outreach.workflow --sheet-id SHEET_ID --sheet-name "Leads"
  python -m workflows.linkedin_outreach.workflow --sheet-id SHEET_ID --add-persona "VP Sales"
  python -m workflows.linkedin_outreach.workflow --sheet-id SHEET_ID --remove-persona "Founder"
  python -m workflows.linkedin_outreach.workflow --sheet-id SHEET_ID --enrich-columns "Employee Count,HQ"
  python -m workflows.linkedin_outreach.workflow --sheet-id SHEET_ID --skip-enrich
  python -m workflows.linkedin_outreach.workflow --sheet-id SHEET_ID --skip-posts
  python -m workflows.linkedin_outreach.workflow --sheet-id SHEET_ID --skip-small-talk
"""

import os
import re
import csv
import json
import time
import argparse
import subprocess
from typing import Optional, List, Dict

import anthropic

from config import CLAUDE_MODEL
from scrapers.web_search.scraper import search_web
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
    from skills.linkedin_copy_writer import skill as _copy_writer_skill  # type: ignore
    _COPY_WRITER_AVAILABLE = True
except Exception:
    _copy_writer_skill = None  # type: ignore
    _COPY_WRITER_AVAILABLE = False

# Enrichment fields. Snake_case keys are used throughout the code; the `label`
# is only used as the default header name when the sheet has no semantic match.
# Add or remove fields here in one place.
ENRICH_FIELDS: List[Dict[str, str]] = [
    {"key": "company_url",         "label": "Company URL",          "desc": "Company website URL (homepage)"},
    {"key": "company_linkedin",    "label": "Company LinkedIn URL", "desc": "Company LinkedIn page URL"},
    {"key": "company_description", "label": "Company Description",  "desc": "One-line description of what the company does"},
    {"key": "employee_count",      "label": "Employee Count",       "desc": "Headcount / number of employees"},
    {"key": "est_revenue",         "label": "Est Revenue",          "desc": "Estimated annual revenue"},
    {"key": "founded_year",        "label": "Founded Year",         "desc": "Year company was founded"},
    {"key": "total_funding",       "label": "Total Funding",        "desc": "Total funding raised"},
    {"key": "hq",                  "label": "HQ",                   "desc": "HQ city"},
]
# Output schema for score_leads — same pattern.
SCORE_FIELDS: List[Dict[str, str]] = [
    {"key": "priority",    "label": "Priority",    "desc": "Priority tier P0 / P1 / P2"},
    {"key": "icp_segment", "label": "ICP Segment", "desc": "ICP segment / tier name from the ICP context"},
    {"key": "reasoning",   "label": "Reasoning",   "desc": "1-2 sentences explaining the priority"},
]

# Fallback scraping defaults if not defined in icp.md
DEFAULT_MAX_POSTS = 15
DEFAULT_DAYS_BACK  = 90


# ---------------------------------------------------------------------------
# Google Sheets helpers
# ---------------------------------------------------------------------------

def gws_read_sheet(sheet_id: str, sheet_name: str) -> List[List[str]]:
    result = subprocess.run(
        [
            "gws", "sheets", "spreadsheets", "values", "get",
            "--params", json.dumps({"spreadsheetId": sheet_id, "range": sheet_name}),
        ],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout).get("values", [])


def gws_write_range(sheet_id: str, range_: str, values: List[List[str]]) -> None:
    subprocess.run(
        [
            "gws", "sheets", "spreadsheets", "values", "update",
            "--params", json.dumps({
                "spreadsheetId": sheet_id,
                "range": range_,
                "valueInputOption": "USER_ENTERED",
            }),
            "--json", json.dumps({"values": values}),
        ],
        capture_output=True, text=True, check=True,
    )


# ---------------------------------------------------------------------------
# Sheet backends — same interface for Google Sheets and CSV
# ---------------------------------------------------------------------------
# Both backends expose:
#   read_all()                        → list of rows (header + data)
#   write_header(col_idx, name)       → write column header
#   write_cell(row_num, col_idx, val) → write one cell (row_num is 1-based, 1=header)
#   write_column(col_idx, values)     → write data rows 2..N+1 in a column

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
    """In-memory rows; rewrites the output CSV on every write so partial progress survives crashes."""

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
        # Pad all rows to the same width for clean CSVs
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
        idx = row_num - 1  # row_num is 1-based
        self._ensure(idx, col_idx)
        self.rows[idx][col_idx] = value
        self._flush()

    def write_column(self, col_idx: int, values: List[str]) -> None:
        for i, v in enumerate(values):
            idx = i + 1  # data starts at row index 1 (sheet row 2)
            self._ensure(idx, col_idx)
            self.rows[idx][col_idx] = v
        self._flush()


# ---------------------------------------------------------------------------
# Sheet column utilities
# ---------------------------------------------------------------------------

def col_letter(idx: int) -> str:
    result = ""
    n = idx + 1
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


def cell(row: List[str], idx: int) -> str:
    return row[idx].strip() if idx < len(row) else ""


# ---------------------------------------------------------------------------
# Context loader + ICP parsers
# ---------------------------------------------------------------------------

def _strip_scaffolding(text: str) -> str:
    """For each `## Section`, if it contains `### Answer`, keep only the
    answer body. Otherwise leave the section as-is (legacy context files)."""
    import re
    out = []
    for m in re.finditer(r"(?ms)^(##\s+[^\n]+)\n(.*?)(?=^##\s+|\Z)", text):
        header, body = m.group(1), m.group(2)
        ans = re.search(r"(?ms)^###\s+Answer\s*$\n(.*?)(?=^###\s+|\Z)", body)
        if ans:
            kept = "\n".join(ln for ln in ans.group(1).splitlines()
                             if not ln.strip().startswith("<!--")).strip()
            if kept and kept.lower() not in ("(fill this in)", "(none)", "(skip)"):
                out.append(f"{header}\n{kept}")
        else:
            stripped = body.strip()
            if stripped:
                out.append(f"{header}\n{stripped}")
    return "\n\n".join(out) if out else text


def load_icp() -> str:
    """Concatenate all .md files in context/ (excluding .example templates)."""
    from config import CONTEXT_DIR
    parts = []
    for fname in sorted(os.listdir(CONTEXT_DIR)):
        if fname.endswith(".md") and ".example" not in fname:
            with open(os.path.join(CONTEXT_DIR, fname)) as f:
                parts.append(_strip_scaffolding(f.read()))
    if not parts:
        raise FileNotFoundError(
            f"No context .md files found in {CONTEXT_DIR}. "
            f"Copy context/context.md.example to context/context.md and fill it in."
        )
    return "\n\n---\n\n".join(parts)


def parse_post_config(icp_context: str) -> Dict:
    """
    Read post scraping config from the ICP markdown.
    Falls back to scraper defaults if the section isn't filled in.
    """
    max_posts = DEFAULT_MAX_POSTS
    days_back  = DEFAULT_DAYS_BACK

    for line in icp_context.splitlines():
        ll = line.lower()
        if "max posts per profile" in ll:
            m = re.search(r'\d+', line)
            if m:
                max_posts = int(m.group())
        elif "days back" in ll:
            m = re.search(r'\d+', line)
            if m:
                days_back = int(m.group())

    return {"max_posts": max_posts, "days_back": days_back}


# ---------------------------------------------------------------------------
# LLM helpers
# ---------------------------------------------------------------------------

def _strip_json_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1]).strip()
    return text


def detect_columns(
    headers: List[str],
    sample_row: List[str],
    required_fields: Dict[str, str],
    client: anthropic.Anthropic,
) -> Dict[str, List[int]]:
    """
    Use Claude to map sheet headers to standard workflow fields.

    `required_fields` is a dict of {field_name: description}.
    Returns {field_name: [col_idx, ...]}. Multiple indices means the values
    should be combined (e.g., firstName + lastName for "name"). Empty list
    means not found.
    """
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
        model=CLAUDE_MODEL, max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )
    parsed = json.loads(_strip_json_fence(resp.content[0].text))
    return {
        k: (v if isinstance(v, list) else [v] if v is not None else [])
        for k, v in parsed.items()
    }


def cell_combined(row: List[str], indices: List[int]) -> str:
    """Join values from multiple columns into a single string (skips empty/missing)."""
    parts = [row[i].strip() for i in indices if i < len(row) and row[i] and row[i].strip()]
    return " ".join(parts)


def find_missing_lead_data(
    lead: Dict[str, str],
    client: anthropic.Anthropic,
) -> Dict[str, str]:
    """
    If name / position / company are missing on a lead, web-search using whatever
    we do have (LinkedIn URL, partial name) and let Claude extract the missing fields.
    Returns the same dict, mutated.
    """
    needed = [k for k in ("name", "position", "company") if not (lead.get(k) or "").strip()]
    if not needed:
        return lead

    query_terms = [lead.get("linkedin"), lead.get("name"), lead.get("company")]
    query = " ".join(t for t in query_terms if (t or "").strip())
    if not query.strip():
        return lead  # nothing to search by

    try:
        result = search_web(
            query=query,
            num_results=3,
            summary_question="Who is this person? What is their full name, current job title, and current company?",
        )
    except Exception as e:
        print(f"    Pre-step search failed for '{query}': {e}")
        return lead

    snippets = []
    for r in result.get("results", []):
        if r.get("summary"):
            snippets.append(r["summary"])
        snippets.extend(r.get("highlights", []))
    if not snippets:
        return lead

    prompt = f"""From the snippets below, identify a single person and extract:
- name: their full name
- position: current job title
- company: current company

Use empty string for fields you can't confidently determine.

Snippets:
{chr(10).join(snippets[:10])}

Return JSON: {{"name": "...", "position": "...", "company": "..."}}
Only valid JSON, no explanation."""

    try:
        resp = client.messages.create(
            model=CLAUDE_MODEL, max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        parsed = json.loads(_strip_json_fence(resp.content[0].text))
    except Exception as e:
        print(f"    Pre-step extract failed: {e}")
        return lead

    for k in needed:
        v = (parsed.get(k) or "").strip()
        if v:
            lead[k] = v
    return lead


def get_or_create_col(headers: List[str], mapping: Dict[str, List[int]], field_key: str, default_name: str) -> int:
    """If `field_key` was mapped to an existing column, use it; otherwise append a new column with `default_name`.
    Guards against the LLM returning out-of-bounds indices."""
    indices = [i for i in (mapping.get(field_key) or []) if 0 <= i < len(headers)]
    if indices:
        return indices[0]
    return ensure_col(headers, default_name)


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

    leads_block = "\n".join(
        f"{i+1}. Name: {l['name']} | Title: {l['position']} | Company: {l['company']}"
        for i, l in enumerate(leads)
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
        model=CLAUDE_MODEL, max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    parsed = json.loads(_strip_json_fence(resp.content[0].text))
    result = [""] * len(leads)
    for item in parsed:
        result[item["index"] - 1] = item["classification"]
    return result


def enrich_company(
    company_name: str,
    fields: List[Dict[str, str]],
    client: anthropic.Anthropic,
) -> Dict[str, str]:
    """
    Enrich a company by web-searching and extracting the requested fields.
    `fields` is a list of {"key", "label", "desc"} dicts. Returns dict keyed by snake_case `key`.
    """
    empty = {f["key"]: "" for f in fields}
    try:
        search_result = search_web(
            query=f"{company_name} company official website linkedin employees revenue funding headquarters founded year",
            num_results=5,
            summary_question=(
                f"What is {company_name}'s official website URL, LinkedIn company page URL, "
                f"employee count, estimated annual revenue, year founded, total funding raised, "
                f"and headquarters location?"
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

Return only valid JSON, no explanation."""

    try:
        resp = client.messages.create(
            model=CLAUDE_MODEL, max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        return json.loads(_strip_json_fence(resp.content[0].text))
    except Exception as e:
        print(f"    LLM extraction failed for {company_name}: {e}")
        return empty


def score_leads(
    leads: List[Dict],
    classifications: List[str],
    icp_context: str,
    client: anthropic.Anthropic,
) -> List[Dict[str, str]]:
    leads_block = "\n".join(
        (
            f"{i+1}. {l['name']} | {l['position']} @ {l['company']} | Persona: {classifications[i]}"
            + (f" | Employees: {l['employee_count']}" if l.get("employee_count") else "")
            + (f" | Revenue: {l['est_revenue']}" if l.get("est_revenue") else "")
            + (f" | Funding: {l['total_funding']}" if l.get("total_funding") else "")
            + (f" | Founded: {l['founded_year']}" if l.get("founded_year") else "")
            + (f" | HQ: {l['hq']}" if l.get("hq") else "")
        )
        for i, l in enumerate(leads)
    )

    prompt = f"""You are a GTM analyst prioritizing sales leads.

ICP Context:
{icp_context}

Priority tiers (use ICP if filled; fall back to general fit signals if empty):
- P0: Best-fit leads. Match ICP tightly. Contact first.
- P1: Good fit with some gaps. Worth pursuing.
- P2: Weak fit or too early. Lower priority.

ICP segments are defined in the ICP Context above. If the context lists named segments
(e.g. "Series-A AI infra", "Mid-market fintech"), assign each lead to the best-fitting
segment. If none are defined, return "" for icp_segment.

Leads:
{leads_block}

For each lead, return:
- index (1-based)
- priority (P0/P1/P2)
- icp_segment (one of the named segments, or "")
- reasoning (1-2 plain sentences — a human salesperson reads this to decide who to contact first; be specific and direct, no filler)

Return only valid JSON, no explanation."""

    resp = client.messages.create(
        model=CLAUDE_MODEL, max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    parsed = json.loads(_strip_json_fence(resp.content[0].text))
    result: List[Dict[str, str]] = [
        {"priority": "", "icp_segment": "", "reasoning": ""} for _ in leads
    ]
    for item in parsed:
        result[item["index"] - 1] = {
            "priority":    item.get("priority", ""),
            "icp_segment": item.get("icp_segment", ""),
            "reasoning":   item.get("reasoning", ""),
        }
    return result


def find_competitors(
    company_name: str,
    client: anthropic.Anthropic,
) -> List[str]:
    """Return 2-3 immediate competitor names for the given company."""
    try:
        search_result = search_web(
            query=f'"{company_name}" direct competitors alternatives similar companies',
            num_results=5,
            summary_question=f"Who are the 2-3 most immediate direct competitors of {company_name}? List company names only.",
        )
    except Exception as e:
        print(f"    Competitor search failed for {company_name}: {e}")
        return []

    snippets = []
    for r in search_result.get("results", []):
        if r.get("summary"):
            snippets.append(r["summary"])
        snippets.extend(r.get("highlights", []))

    if not snippets:
        return []

    prompt = f"""From this research about {company_name}'s competitive landscape, extract 2-3 immediate direct competitors.

Research:
{chr(10).join(snippets[:15])}

Return a JSON array of company name strings only: ["Company A", "Company B"]
Pick the most directly comparable companies — not adjacent categories.
Return only valid JSON, no explanation."""

    try:
        resp = client.messages.create(
            model=CLAUDE_MODEL, max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        return json.loads(_strip_json_fence(resp.content[0].text))
    except Exception as e:
        print(f"    Competitor extraction failed for {company_name}: {e}")
        return []


def scrape_and_filter_posts(
    profile_url: str,
    icp_context: str,
    max_posts: int,
    days_back: int,
    client: anthropic.Anthropic,
) -> Dict:
    """
    Scrape LinkedIn posts for a profile URL and filter by ICP criteria.

    Returns:
        {
          "urls":       [...],   # matching post URLs — written to sheet (links only)
          "posts_data": [...],   # matching post dicts (url, text, posted_at) — kept in memory for Step 8
        }
    """
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

    prompt = f"""You are evaluating LinkedIn posts to identify which ones are relevant for sales outreach.

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
            model=CLAUDE_MODEL, max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        matching = json.loads(_strip_json_fence(resp.content[0].text))
    except Exception as e:
        print(f"    Post filtering failed: {e}")
        matching = list(range(1, len(posts) + 1))  # fallback: keep all

    valid = [
        i for i in matching
        if isinstance(i, int) and 1 <= i <= len(posts)
    ]

    matched_posts = [posts[i - 1] for i in valid]
    return {
        "urls": [p["url"] for p in matched_posts if p.get("url")],
        "posts_data": [
            {"url": p.get("url", ""), "text": p.get("text", ""), "posted_at": p.get("posted_at", "")}
            for p in matched_posts
        ],
    }


def generate_personalisation_hooks(
    name: str,
    company: str,
    position: str,
    matching_posts: List[Dict],
    small_talk: str,
    icp_context: str,
    **kwargs,
) -> str:
    """
    Generate personalisation talking points for a lead using the Personalisation Hook Skill.

    Returns a string of talking points suitable for the 'Personalisation Hook' column.
    Returns empty string if the skill is not yet available or the call fails.

    Expected skill location: Skills/Personalisation Hook/skill.py
    Expected interface:
        generate_hooks(name, company, position, matching_posts, small_talk, icp_context) -> dict
        returns {"hooks": "talking points string", "errors": [...]}
    """
    if not _PERSONALISATION_AVAILABLE:
        return ""

    extras = kwargs or {}
    try:
        result = _personalisation_skill.generate_hooks(
            name=name,
            company=company,
            position=position,
            matching_posts=matching_posts,
            small_talk=small_talk,
            icp_context=icp_context,
            competitors=extras.get("competitors") or [],
            company_description=extras.get("company_description", ""),
            employee_count=extras.get("employee_count", ""),
            est_revenue=extras.get("est_revenue", ""),
            total_funding=extras.get("total_funding", ""),
            hq=extras.get("hq", ""),
        )
        return result.get("hooks", "")
    except Exception as e:
        print(f"    Personalisation hook failed for {name}: {e}")
        return ""


def write_linkedin_copy(
    name: str,
    company: str,
    position: str,
    buyer_persona: str,
    priority: str,
    competitors: List[str],
    matching_posts: List[Dict],
    small_talk: str,
    personalisation_hook: str,
    icp_context: str,
    employee_count: str,
    est_revenue: str,
    total_funding: str,
    hq: str,
) -> str:
    """
    Write a personalised LinkedIn outreach message using the LinkedIn Copy Writer Skill.

    Returns the message as a string, or empty string if the skill is not available.

    Skill location: skills/linkedin_copy_writer/skill.py
    Interface:
        write_copy(**kwargs) -> dict
        returns {"copy": "message text", "signal_used": str, "review": dict, "errors": [...]}
    """
    if not _COPY_WRITER_AVAILABLE:
        return ""

    try:
        result = _copy_writer_skill.write_copy(
            name=name,
            company=company,
            position=position,
            buyer_persona=buyer_persona,
            priority=priority,
            competitors=competitors,
            matching_posts=matching_posts,
            small_talk=small_talk,
            personalisation_hook=personalisation_hook,
            icp_context=icp_context,
            employee_count=employee_count,
            est_revenue=est_revenue,
            total_funding=total_funding,
            hq=hq,
        )
        return result.get("copy", "")
    except Exception as e:
        print(f"    Copy writer failed for {name}: {e}")
        return ""


def scrape_small_talk(
    profile_url: str,
    name: str,
    company: str,
) -> str:
    """
    Fetch personalisation / small-talk details for a lead using the Small Talk Scraper.

    Returns a short string (1-3 sentences) suitable for writing to the 'Small Talk' column.
    Returns an empty string if the scraper is not yet available or the call fails.

    The Small Talk Scraper is not yet built — this function acts as the integration point.
    Once Scrapers/Small Talk Scraper/scraper.py is created and exposes a
    `scrape_small_talk(profile_url, name, company)` function returning a dict with a
    `small_talk` key, this will work automatically.
    """
    if not _SMALL_TALK_AVAILABLE:
        return ""

    try:
        result = _small_talk_scraper.scrape_small_talk(
            profile_url=profile_url,
            name=name,
            company=company,
        )
        return result.get("small_talk", "")
    except Exception as e:
        print(f"    Small talk scraper failed for {name}: {e}")
        return ""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _apply_enrichment(lead: Dict, enriched: Dict[str, str]) -> None:
    """Copy all enrichment values into the lead dict using snake_case keys."""
    for f in ENRICH_FIELDS:
        lead[f["key"]] = enriched.get(f["key"], "")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="LinkedIn Outreach Workflow — Qualify, Enrich, Prioritize, Prep Outreach"
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--sheet-id", default=None,
                     help="Google Sheet ID (from the URL). Requires the gws CLI to be installed and authed.")
    src.add_argument("--input-csv", default=None,
                     help="Path to a CSV file of leads. Use this if you don't want to use Google Sheets.")
    parser.add_argument("--sheet-name", default="Sheet1",
                        help="(Sheets only) Sheet tab name. Default: Sheet1")
    parser.add_argument("--output-csv", default=None,
                        help="(CSV only) Where to write enriched output. Defaults to overwriting --input-csv.")
    parser.add_argument("--add-persona", action="append", default=[], metavar="TITLE",
                        help="One-time: also treat this title as a buyer (can repeat)")
    parser.add_argument("--remove-persona", action="append", default=[], metavar="TITLE",
                        help="One-time: exclude this title from buyer consideration (can repeat)")
    parser.add_argument("--enrich-fields", default=None,
                        help=f"Comma-separated enrichment field keys (default: {', '.join(f['key'] for f in ENRICH_FIELDS)})")
    parser.add_argument("--include-p1", action="store_true",
                        help="Also include P1 leads in the outreach batch (default: P0 only)")
    parser.add_argument("--include-p2", action="store_true",
                        help="Also include P2 leads in the outreach batch (default: P0 only)")
    parser.add_argument("--skip-enrich", action="store_true",
                        help="Skip company enrichment step (Step 2)")
    parser.add_argument("--skip-posts", action="store_true",
                        help="Skip LinkedIn post scraping step (Step 6)")
    parser.add_argument("--skip-small-talk", action="store_true",
                        help="Skip small talk personalisation step (Step 7)")
    parser.add_argument("--skip-copy", action="store_true",
                        help="Skip LinkedIn copy writing step (Step 9)")
    args = parser.parse_args()

    client = anthropic.Anthropic()

    if args.skip_enrich:
        enrich_fields: List[Dict[str, str]] = []
    elif args.enrich_fields:
        wanted = {k.strip() for k in args.enrich_fields.split(",")}
        enrich_fields = [f for f in ENRICH_FIELDS if f["key"] in wanted]
    else:
        enrich_fields = list(ENRICH_FIELDS)

    # ------------------------------------------------------------------
    # Load context
    # ------------------------------------------------------------------
    print("\nLoading ICP context from context/...")
    icp_context = load_icp()
    post_config  = parse_post_config(icp_context)
    print(f"  Post scraping config: max_posts={post_config['max_posts']}, days_back={post_config['days_back']}")

    # ------------------------------------------------------------------
    # Pick a backend (Google Sheet or CSV) and read all rows
    # ------------------------------------------------------------------
    if args.sheet_id:
        backend = GoogleSheetsBackend(args.sheet_id, args.sheet_name)
        print(f"Reading sheet {args.sheet_id} tab '{args.sheet_name}'...")
    else:
        backend = CsvBackend(args.input_csv, args.output_csv)
        out = args.output_csv or args.input_csv
        print(f"Reading CSV {args.input_csv} (writing to {out})...")
    rows = backend.read_all()
    if not rows or len(rows) < 2:
        print("Sheet is empty or has no data rows. Exiting.")
        return

    headers: List[str] = list(rows[0])
    data_rows: List[List[str]] = rows[1:]
    print(f"Found {len(data_rows)} leads.")

    print("\nDetecting columns via Claude...")
    mapping = detect_columns(
        headers,
        data_rows[0] if data_rows else [],
        {
            # inputs
            "name":           "Full person name (combine first + last name columns if split)",
            "company":        "Current company name (not LinkedIn URL — just the name)",
            "linkedin":       "LinkedIn profile URL of the person (not the company's LinkedIn)",
            "position":       "Job title / role / position",
            # outputs (ok if missing — will be appended)
            "buyer_persona":  "Buyer-persona classification: Decision Maker / Champion / Non DM",
            "priority":       "Priority tier: P0 / P1 / P2",
            "reasoning":      "Reasoning text explaining the priority",
            "competitors":    "List of competitor companies of the lead's company",
            "company_url":         "Company website URL (homepage)",
            "company_linkedin":    "Company LinkedIn page URL",
            "company_description": "One-line description of what the company does",
            "employee_count":      "Headcount / number of employees at the company",
            "est_revenue":         "Estimated annual revenue of the company",
            "founded_year":        "Year company was founded",
            "total_funding":       "Total funding raised by the company",
            "hq":                  "HQ city",
            "icp_segment":         "ICP segment / tier the company belongs to",
            "post_links":     "LinkedIn post URLs of this person",
            "small_talk":     "Small-talk / personalisation details about the person",
            "hooks":          "Talking points / personalisation hooks for outreach",
            "copy":           "Final outreach message / copy",
        },
        client,
    )
    print(f"  Mapping: {mapping}")

    if not mapping.get("name") or not mapping.get("company") or not mapping.get("position"):
        print("ERROR: Could not detect required input columns from sheet.")
        print(f"Headers: {headers}")
        return

    leads: List[Dict] = []
    for row in data_rows:
        leads.append({
            "name":     cell_combined(row, mapping.get("name", [])),
            "company":  cell_combined(row, mapping.get("company", [])),
            "linkedin": cell_combined(row, mapping.get("linkedin", [])),
            "position": cell_combined(row, mapping.get("position", [])),
        })

    # ------------------------------------------------------------------
    # Pre-step: fill in missing name / position / company via web search
    # (rare; only fires for rows with one or more required fields blank)
    # ------------------------------------------------------------------
    incomplete = [i for i, l in enumerate(leads)
                  if not all((l.get(k) or "").strip() for k in ("name", "position", "company"))]
    if incomplete:
        print(f"\n--- Pre-step: Filling missing data for {len(incomplete)} lead(s) ---")
        for i in incomplete:
            before = {k: leads[i].get(k, "") for k in ("name", "position", "company")}
            find_missing_lead_data(leads[i], client)
            after = {k: leads[i].get(k, "") for k in ("name", "position", "company")}
            filled = {k: after[k] for k in after if not before[k] and after[k]}
            if filled:
                print(f"  Row {i+2}: filled {filled}")
            else:
                print(f"  Row {i+2}: nothing found")

    # ------------------------------------------------------------------
    # Step 1: Classify buyer personas
    # ------------------------------------------------------------------
    print("\n--- Step 1: Classifying buyer personas ---")
    if args.add_persona:
        print(f"  + Adding for this run: {args.add_persona}")
    if args.remove_persona:
        print(f"  - Removing for this run: {args.remove_persona}")

    classifications = classify_personas(leads, icp_context, args.add_persona, args.remove_persona, client)

    buyer_col_idx = get_or_create_col(headers, mapping, "buyer_persona", "Buyer Persona Match")
    backend.write_header(buyer_col_idx, headers[buyer_col_idx])
    backend.write_column(buyer_col_idx, classifications)

    dm_count    = sum(1 for c in classifications if c == "Decision Maker")
    champ_count = sum(1 for c in classifications if c == "Champion")
    ndm_count   = sum(1 for c in classifications if c == "Non Decision Maker")
    print(f"  Decision Makers: {dm_count} | Champions: {champ_count} | Non DMs: {ndm_count}")
    print(f"  Written to column {col_letter(buyer_col_idx)}")

    # ------------------------------------------------------------------
    # Step 2: Enrich Decision Maker companies
    # ------------------------------------------------------------------
    dm_indices = [i for i, c in enumerate(classifications) if c == "Decision Maker"]

    if dm_indices and enrich_fields:
        print(f"\n--- Step 2: Enriching {len(dm_indices)} Decision Maker company(ies) ---")
        print(f"  Fields: {', '.join(f['key'] for f in enrich_fields)}")

        # For each field: locate (or create) the destination column on the sheet.
        col_idx_by_key: Dict[str, int] = {}
        for f in enrich_fields:
            idx = get_or_create_col(headers, mapping, f["key"], f["label"])
            col_idx_by_key[f["key"]] = idx
            backend.write_header(idx, headers[idx])

        company_cache: Dict[str, Dict[str, str]] = {}

        for i in dm_indices:
            lead    = leads[i]
            company = lead["company"]
            row     = data_rows[i]
            row_num = i + 2

            all_filled = all(
                col_idx_by_key[f["key"]] < len(row) and row[col_idx_by_key[f["key"]]].strip()
                for f in enrich_fields
            )
            if all_filled:
                print(f"  Skipping {company} — already filled")
                enriched = {f["key"]: cell(row, col_idx_by_key[f["key"]]) for f in enrich_fields}
                company_cache[company] = enriched
                _apply_enrichment(leads[i], enriched)
                continue

            if company in company_cache:
                enriched = company_cache[company]
            else:
                print(f"  Enriching: {company}...")
                enriched = enrich_company(company, enrich_fields, client)
                company_cache[company] = enriched

            for f in enrich_fields:
                backend.write_cell(row_num, col_idx_by_key[f["key"]], enriched.get(f["key"], ""))

            _apply_enrichment(leads[i], enriched)
            print(f"    {company}: {json.dumps(enriched, ensure_ascii=False)}")

    elif not enrich_fields:
        print("\n--- Step 2: Skipping enrichment (--skip-enrich) ---")
    else:
        print("\n--- Step 2: No Decision Makers — skipping enrichment ---")

    # ------------------------------------------------------------------
    # Step 3: Score all leads P0 / P1 / P2
    # ------------------------------------------------------------------
    print("\n--- Step 3: Scoring all leads P0 / P1 / P2 ---")
    scores = score_leads(leads, classifications, icp_context, client)

    priority_col_idx  = get_or_create_col(headers, mapping, "priority",    "Priority")
    icp_col_idx       = get_or_create_col(headers, mapping, "icp_segment", "ICP Segment")
    reasoning_col_idx = get_or_create_col(headers, mapping, "reasoning",   "Reasoning")

    backend.write_header(priority_col_idx,  headers[priority_col_idx])
    backend.write_header(icp_col_idx,       headers[icp_col_idx])
    backend.write_header(reasoning_col_idx, headers[reasoning_col_idx])
    backend.write_column(priority_col_idx,  [s["priority"] for s in scores])
    backend.write_column(icp_col_idx,       [s.get("icp_segment", "") for s in scores])
    backend.write_column(reasoning_col_idx, [s["reasoning"] for s in scores])

    p0 = sum(1 for s in scores if s["priority"] == "P0")
    p1 = sum(1 for s in scores if s["priority"] == "P1")
    p2 = sum(1 for s in scores if s["priority"] == "P2")
    print(f"  P0: {p0} | P1: {p1} | P2: {p2}")
    print(f"  Priority col {priority_col_idx} | ICP Segment col {icp_col_idx} | Reasoning col {reasoning_col_idx}")

    # ------------------------------------------------------------------
    # Step 4: Select outreach batch (P0 only by default; opt-in via --include-p1/p2)
    # ------------------------------------------------------------------
    print("\n--- Step 4: Selecting outreach batch ---")
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

    # ------------------------------------------------------------------
    # In-memory caches threaded across the remaining steps
    # ------------------------------------------------------------------
    competitors_by_lead: Dict[int, List[str]] = {}  # populated in Step 5
    post_data_by_lead:   Dict[int, List[Dict]] = {}  # populated in Step 6
    small_talk_by_lead:  Dict[int, str]        = {}  # populated in Step 7
    hooks_by_lead:       Dict[int, str]        = {}  # populated in Step 8

    # ------------------------------------------------------------------
    # Step 5: Find competitors for each filtered lead's company
    # ------------------------------------------------------------------
    print(f"\n--- Step 5: Finding competitors for {len(outreach_indices)} leads ---")

    competitors_col_idx = get_or_create_col(headers, mapping, "competitors", "Competitors")
    backend.write_header(competitors_col_idx, headers[competitors_col_idx])

    competitor_cache: Dict[str, List[str]] = {}

    for i in outreach_indices:
        company = leads[i]["company"]
        row_num = i + 2

        if company in competitor_cache:
            comps = competitor_cache[company]
        else:
            print(f"  {company}...")
            comps = find_competitors(company, client)
            competitor_cache[company] = comps

        competitors_by_lead[i] = comps  # keep in memory for Step 9

        backend.write_cell(row_num, competitors_col_idx, ", ".join(comps))
        if comps:
            print(f"    → {', '.join(comps)}")

    # ------------------------------------------------------------------
    # Step 6: Scrape + filter LinkedIn posts
    # ------------------------------------------------------------------
    if args.skip_posts:
        print("\n--- Step 6: Skipping post scraping (--skip-posts) ---")
    else:
        print(f"\n--- Step 6: Scraping LinkedIn posts for {len(outreach_indices)} leads ---")
        print(f"  Config: {post_config['max_posts']} posts / {post_config['days_back']} days")

        post_links_col_idx = get_or_create_col(headers, mapping, "post_links", "LinkedIn Post Links")
        backend.write_header(post_links_col_idx, headers[post_links_col_idx])

        # post_data_by_lead is declared above — populated here for Step 8

        for i in outreach_indices:
            lead         = leads[i]
            linkedin_url = lead.get("linkedin", "")
            row_num      = i + 2

            if not linkedin_url:
                print(f"  {lead['name']} — no LinkedIn URL, skipping")
                post_data_by_lead[i] = []
                continue

            print(f"  {lead['name']} ({lead['company']})...")
            post_result = scrape_and_filter_posts(
                profile_url=linkedin_url,
                icp_context=icp_context,
                max_posts=post_config["max_posts"],
                days_back=post_config["days_back"],
                client=client,
            )

            post_data_by_lead[i] = post_result["posts_data"]  # full text kept in memory

            cell_value = "\n".join(post_result["urls"]) if post_result["urls"] else ""
            backend.write_cell(row_num, post_links_col_idx, cell_value)
            print(f"    {len(post_result['urls'])} post(s) matched")
            time.sleep(5)  # avoid LinkedIn rate-limiting the actor session on bulk runs

    # ------------------------------------------------------------------
    # Step 7: Small talk personalisation
    # ------------------------------------------------------------------
    if args.skip_small_talk:
        print("\n--- Step 7: Skipping small talk (--skip-small-talk) ---")
    elif not _SMALL_TALK_AVAILABLE:
        print("\n--- Step 7: Small Talk Scraper failed to import — skipping ---")
        print("  (Create Scrapers/Small Talk Scraper/scraper.py to enable this step)")
    else:
        print(f"\n--- Step 7: Gathering small talk details for {len(outreach_indices)} leads ---")

        small_talk_col_idx = get_or_create_col(headers, mapping, "small_talk", "Small Talk")
        backend.write_header(small_talk_col_idx, headers[small_talk_col_idx])

        # small_talk_by_lead is declared above — populated here for Step 8

        for i in outreach_indices:
            lead         = leads[i]
            linkedin_url = lead.get("linkedin", "")
            row_num      = i + 2

            print(f"  {lead['name']} ({lead['company']})...")
            detail = scrape_small_talk(
                profile_url=linkedin_url,
                name=lead["name"],
                company=lead["company"],
            )

            small_talk_by_lead[i] = detail  # keep in memory for Step 8

            backend.write_cell(row_num, small_talk_col_idx, detail)
            if detail:
                print(f"    → {detail[:100]}{'...' if len(detail) > 100 else ''}")
            else:
                print("    → (no detail found)")

    # ------------------------------------------------------------------
    # Step 8: Personalisation hooks
    # ------------------------------------------------------------------
    if args.skip_small_talk and args.skip_posts:
        # Both data sources were skipped — nothing to personalise from
        print("\n--- Step 8: Skipping personalisation hooks (no post or small talk data) ---")
    elif not _PERSONALISATION_AVAILABLE:
        print("\n--- Step 8: Personalisation Hook Skill failed to import — skipping ---")
    else:
        print(f"\n--- Step 8: Generating personalisation hooks for {len(outreach_indices)} leads ---")

        hooks_col_idx = get_or_create_col(headers, mapping, "hooks", "Personalisation Hook")
        backend.write_header(hooks_col_idx, headers[hooks_col_idx])

        for i in outreach_indices:
            lead    = leads[i]
            row_num = i + 2

            print(f"  {lead['name']} ({lead['company']})...")
            hooks = generate_personalisation_hooks(
                name=lead["name"],
                company=lead["company"],
                position=lead["position"],
                matching_posts=post_data_by_lead.get(i, []),
                small_talk=small_talk_by_lead.get(i, ""),
                icp_context=icp_context,
                competitors=competitors_by_lead.get(i, []),
                company_description=lead.get("company_description", ""),
                employee_count=lead.get("employee_count", ""),
                est_revenue=lead.get("est_revenue", ""),
                total_funding=lead.get("total_funding", ""),
                hq=lead.get("hq", ""),
            )

            hooks_by_lead[i] = hooks  # keep in memory for Step 9

            backend.write_cell(row_num, hooks_col_idx, hooks)
            if hooks:
                print(f"    → {hooks[:120]}{'...' if len(hooks) > 120 else ''}")
            else:
                print("    → (no hooks generated)")

    # ------------------------------------------------------------------
    # Step 9: Write LinkedIn copy
    # ------------------------------------------------------------------
    if args.skip_copy:
        print("\n--- Step 9: Skipping LinkedIn copy (--skip-copy) ---")
    elif not _COPY_WRITER_AVAILABLE:
        print("\n--- Step 9: LinkedIn Copy Writer Skill failed to import — skipping ---")
        print("  (Check skills/linkedin_copy_writer/skill.py and its dependencies)")
    else:
        print(f"\n--- Step 9: Writing LinkedIn copy for {len(outreach_indices)} leads ---")

        copy_col_idx = get_or_create_col(headers, mapping, "copy", "LinkedIn Copy")
        backend.write_header(copy_col_idx, headers[copy_col_idx])

        for i in outreach_indices:
            lead    = leads[i]
            row_num = i + 2

            print(f"  {lead['name']} ({lead['company']})...")
            copy = write_linkedin_copy(
                name=lead["name"],
                company=lead["company"],
                position=lead["position"],
                buyer_persona=classifications[i],
                priority=scores[i]["priority"],
                competitors=competitors_by_lead.get(i, []),
                matching_posts=post_data_by_lead.get(i, []),
                small_talk=small_talk_by_lead.get(i, ""),
                personalisation_hook=hooks_by_lead.get(i, ""),
                icp_context=icp_context,
                employee_count=lead.get("employee_count", ""),
                est_revenue=lead.get("est_revenue", ""),
                total_funding=lead.get("total_funding", ""),
                hq=lead.get("hq", ""),
            )

            backend.write_cell(row_num, copy_col_idx, copy)
            if copy:
                print(f"    → {copy[:120]}{'...' if len(copy) > 120 else ''}")
            else:
                print("    → (no copy generated)")

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    print("\n======= Done =======")
    print(f"  Total leads processed:  {len(leads)}")
    print(f"  Buyer personas:         {dm_count} DMs | {champ_count} Champions | {ndm_count} Non DMs")
    print(f"  Priority scores:        {p0} P0 | {p1} P1 | {p2} P2")
    print(f"  Outreach batch:         {len(outreach_indices)} leads")


if __name__ == "__main__":
    main()
