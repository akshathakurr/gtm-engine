"""Per-lead steps for the LinkedIn outreach workflow.

Qualification, enrichment, scoring, competitor lookup, post scraping/filtering,
small talk, personalisation hooks, and copy writing. Each function takes plain
inputs and returns raw values; orchestration and sheet/CSV I/O live in
``workflow.py``.
"""

import json
from typing import List, Dict

import anthropic

from config import CLAUDE_MODEL, cached_system
from workflows._common import (
    strip_json_fence as _strip_json_fence,
    map_rate_limited,
    collect_chunk_results,
)
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


# Concurrency. Per-lead compute (Claude + Exa + Apify) runs in a bounded pool;
# the backend is written sequentially on the main thread afterwards, so it is
# never touched concurrently. Throttled services use a rate limiter that spaces
# call *starts* by a fixed interval.
ENRICH_CONCURRENCY = 6        # Claude/Exa per-lead steps
POSTS_MIN_INTERVAL = 5        # seconds between profile-posts runs (step 6)
POSTS_CONCURRENCY = 3
# Classify/score run one LLM call per chunk of leads (not one call for the whole
# sheet). Chunking prevents (a) JSON truncation on large sheets — 1000 leads
# overrun max_tokens and later leads come back blank — and (b) batch-context
# drift, where the same lead is classified differently depending on its neighbours.
LLM_BATCH_SIZE = 40


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
            model=CLAUDE_MODEL, temperature=0, max_tokens=200,
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
        # Static instructions + ICP + per-run overrides go in a cached system
        # prefix; only the per-chunk leads list varies between calls.
        system = f"""You are a B2B sales analyst classifying leads by buyer role.

ICP / Buyer Persona Context:
{icp_context}
{overrides}

Definitions (use ICP context to map titles; fall back to general B2B conventions if ICP is empty):
- Decision Maker: Has budget authority and can sign/approve a deal.
- Champion: Influences the buying decision but cannot sign alone.
- Non Decision Maker: Not involved in the buying decision.

Return a JSON array — one object per lead — with "index" (1-based) and "classification".
Only return valid JSON, no explanation."""

        prompt = f"Leads:\n{leads_block}"

        resp = client.messages.create(
            model=CLAUDE_MODEL, temperature=0, max_tokens=2000,
            system=cached_system(system),
            messages=[{"role": "user", "content": prompt}],
        )
        parsed = json.loads(_strip_json_fence(resp.content[0].text))
        out = [""] * len(chunk)
        for item in parsed:
            idx = item.get("index", 0) - 1
            if 0 <= idx < len(chunk):
                out[idx] = item.get("classification", "")
        return out

    # One call per chunk keeps each response small (no truncation) and each
    # lead judged against only its chunk. Chunks are independent → run in parallel.
    chunks = [leads[i:i + LLM_BATCH_SIZE] for i in range(0, len(leads), LLM_BATCH_SIZE)]
    chunk_results, chunk_errors = map_rate_limited(_classify_chunk, chunks, max_workers=ENRICH_CONCURRENCY)
    return collect_chunk_results(
        chunks, chunk_results, chunk_errors, label="persona classification",
        blank=lambda chunk: [""] * len(chunk),
    )


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
        # Cost control: 3 results is plenty for firmographics (homepage +
        # LinkedIn + a Crunchbase/Wikipedia-style page) and keeps the Claude
        # extraction input small — the enrichment call is the workflow's
        # biggest token consumer.
        search_result = search_web(
            query=f"{company_name} company official website linkedin employees revenue funding headquarters founded year",
            num_results=3,
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
{chr(10).join(snippets[:6])}

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
            model=CLAUDE_MODEL, temperature=0, max_tokens=500,
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
    def _score_chunk(pairs: List[tuple]) -> List[Dict[str, str]]:
        # pairs: list of (lead, classification), 1-based indexing within the chunk
        leads_block = "\n".join(
            (
                f"{i+1}. {l['name']} | {l['position']} @ {l['company']} | Persona: {cls}"
                + (f" | Employees: {l['employee_count']}" if l.get("employee_count") else "")
                + (f" | Revenue: {l['est_revenue']}" if l.get("est_revenue") else "")
                + (f" | Funding: {l['total_funding']}" if l.get("total_funding") else "")
                + (f" | Founded: {l['founded_year']}" if l.get("founded_year") else "")
                + (f" | HQ: {l['hq']}" if l.get("hq") else "")
            )
            for i, (l, cls) in enumerate(pairs)
        )

        # Static instructions + ICP go in a cached system prefix (identical across
        # every chunk); only the per-chunk leads list rides in the user message.
        system = f"""You are a GTM analyst prioritizing sales leads.

ICP Context:
{icp_context}

Priority tiers (use ICP if filled; fall back to general fit signals if empty):
- P0: Best-fit leads. Match ICP tightly. Contact first.
- P1: Good fit with some gaps. Worth pursuing.
- P2: Weak fit or too early. Lower priority.

ICP segments are defined in the ICP Context above. If the context lists named segments
(e.g. "Series-A AI infra", "Mid-market fintech"), assign each lead to the best-fitting
segment. If none are defined, return "" for icp_segment.

For each lead, return:
- index (1-based)
- priority (P0/P1/P2)
- icp_segment (one of the named segments, or "")
- reasoning (1-2 plain sentences — a human salesperson reads this to decide who to contact first; be specific and direct, no filler)

Return only valid JSON, no explanation."""

        prompt = f"Leads:\n{leads_block}"

        resp = client.messages.create(
            model=CLAUDE_MODEL, temperature=0, max_tokens=4000,
            system=cached_system(system),
            messages=[{"role": "user", "content": prompt}],
        )
        parsed = json.loads(_strip_json_fence(resp.content[0].text))
        out: List[Dict[str, str]] = [
            {"priority": "", "icp_segment": "", "reasoning": ""} for _ in pairs
        ]
        for item in parsed:
            idx = item.get("index", 0) - 1
            if 0 <= idx < len(pairs):
                out[idx] = {
                    "priority":    item.get("priority", ""),
                    "icp_segment": item.get("icp_segment", ""),
                    "reasoning":   item.get("reasoning", ""),
                }
        return out

    # One call per chunk — avoids JSON truncation on large sheets and keeps each
    # lead scored against only its chunk. Chunks are independent → run in parallel.
    all_pairs = list(zip(leads, classifications))
    chunks = [all_pairs[i:i + LLM_BATCH_SIZE] for i in range(0, len(all_pairs), LLM_BATCH_SIZE)]
    chunk_results, chunk_errors = map_rate_limited(_score_chunk, chunks, max_workers=ENRICH_CONCURRENCY)
    return collect_chunk_results(
        chunks, chunk_results, chunk_errors, label="lead scoring",
        blank=lambda chunk: [{"priority": "", "icp_segment": "", "reasoning": ""} for _ in chunk],
    )


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
            model=CLAUDE_MODEL, temperature=0, max_tokens=200,
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
            model=CLAUDE_MODEL, temperature=0, max_tokens=300,
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
        hooks = result.get("hooks", "")
        skill_errors = result.get("errors") or []
        if skill_errors and not hooks:
            print(f"    No hook for {name}: {'; '.join(skill_errors)}")
        return hooks
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
