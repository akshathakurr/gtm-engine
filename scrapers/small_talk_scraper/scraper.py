"""
Small Talk Intelligence Scraper

Finds humanizing, conversational details about a prospect that can be used
naturally as small talk in cold outbound. Optimizes for HUMANITY, not enrichment.

Pipeline (web-search only — no Twitter/Apify):
  1. Query generation — Claude writes precise, source-targeted queries aimed at
                        casual/personal content (interviews, podcasts, blogs,
                        Reddit, YouTube) — never generic "CEO of X" queries.
  2. Signal harvest   — run those web searches and collect the results.
  3. Extraction+score — Claude pulls humanizing signals (hobbies, fandoms,
                        quirks, humor, routines), verifies each is the SAME
                        person, scores small-talk-worthiness; top 2-3 win.
  4. Output           — 2-3 line string + structured signals + identity.

Twitter/X scraping was removed on purpose: it added a paid Apify pull plus an
identity-resolution web search per lead, for a source that's often empty or
wrong-person. The person's LinkedIn activity already flows into the copy step
(via the posts scraper), so here we lean on precise web search instead.

Workflow-facing entrypoint: scrape_small_talk(profile_url, name, company, ...)
returns {"small_talk": str, "signals": [...], "identity": {...}}.
"""

import os
import sys
import json
import re
from typing import Optional, List, Dict, Any

import anthropic

# Make sibling scrapers importable when run as a module from repo root
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from config import CLAUDE_MODEL, cached_system  # noqa: E402
from scrapers.web_search.scraper import search_web_batch  # noqa: E402


# Cost control: this scraper fires per-lead web searches + Claude calls, so it
# multiplies fast across a list. Keep the fan-out modest.
DEFAULT_NUM_SEARCH_QUERIES = 3
DEFAULT_RESULTS_PER_QUERY = 4
DEFAULT_MAX_SIGNALS = 3


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _strip_json_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    text = text.strip()
    # Tolerate prose preamble/suffix (and trailing "Extra data") around the JSON:
    # carve out the outermost object or array.
    if text and text[0] not in "{[":
        starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
        if starts:
            text = text[min(starts):]
    if text and text[-1] not in "}]":
        ends = [i for i in (text.rfind("}"), text.rfind("]")) if i != -1]
        if ends:
            text = text[:max(ends) + 1]
    return text.strip()


def _claude_json(client: anthropic.Anthropic, prompt: str, max_tokens: int = 1500,
                 system: Optional[str] = None) -> Any:
    """Call Claude and parse the first JSON value from the reply.

    ``system``, if given, is sent as a prompt-cached ``system=`` prefix — pass the
    invariant instructions there (not the per-lead data) so a run over many leads
    pays for that prefix once instead of re-billing it every call.
    """
    kwargs: Dict[str, Any] = {
        "model": CLAUDE_MODEL,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = cached_system(system)
    resp = client.messages.create(**kwargs)
    text = _strip_json_fence(resp.content[0].text)
    # raw_decode parses the first JSON value and ignores any trailing text
    # (Claude sometimes appends a note after the JSON → "Extra data" on loads).
    return json.JSONDecoder().raw_decode(text)[0]


# ---------------------------------------------------------------------------
# Stage 1 — Identity (no lookup — just the anchors we already have)
# ---------------------------------------------------------------------------

def build_identity(
    name: str,
    company: str,
    linkedin_url: str = "",
    website: str = "",
) -> Dict[str, Any]:
    """Assemble the identity anchors used to verify sources in extraction.

    No search/LLM here: without Twitter there's nothing to resolve — the caller
    already knows the name, company, and (usually) LinkedIn URL.
    """
    return {
        "name": name,
        "company": company,
        "linkedin_url": linkedin_url,
        "website": website,
    }


# ---------------------------------------------------------------------------
# Stage 2 — Signal harvest (web search only)
# ---------------------------------------------------------------------------

SIGNAL_CATEGORIES = [
    "hobbies, sports, fandoms (e.g. F1, Arsenal, anime, climbing)",
    "gaming, music, books, movies, podcasts they like",
    "internet behavior — meme posting, humor style, recurring jokes",
    "lifestyle — coffee, fitness, travel, routines, weekend activities",
    "side projects, weird quirks, unusual opinions, identity signals",
]


def generate_search_queries(
    name: str,
    company: str,
    client: anthropic.Anthropic,
    n: int = DEFAULT_NUM_SEARCH_QUERIES,
) -> List[str]:
    """Have Claude generate precise, source-targeted queries for humanizing signals."""
    cats = "\n".join(f"- {c}" for c in SIGNAL_CATEGORIES)
    prompt = f"""Generate {n} web search queries to find HUMANIZING details about a person —
hobbies, fandoms, quirks, humor, casual interests. NOT business/professional info.

Person: {name}
Company: {company}

Aim queries at categories like:
{cats}

Be PRECISE about where to look — target casual/personal content where the person
speaks conversationally: podcast appearances, long-form interviews, personal
blogs, Reddit threads, YouTube. Prefer queries naming those source types
explicitly (e.g. "{name} podcast interview hobbies", "{name} personal blog").
AVOID generic queries like "{name} CEO" or "{name} {company} funding".

Return JSON array of {n} strings. Only valid JSON."""

    try:
        queries = _claude_json(client, prompt, max_tokens=600)
        if isinstance(queries, list) and queries:
            return [str(q) for q in queries][:n]
    except Exception as e:
        print(f"  query generation failed: {e}", file=sys.stderr)

    # Fallback queries — precise source types, no LLM needed.
    base = f'"{name}" {company}'
    return [
        f"{base} podcast interview",
        f'"{name}" hobbies OR interests personal',
        f"{base} personal blog OR reddit",
        f'"{name}" youtube interview',
        f"{base} weekend OR side project",
    ][:n]


def harvest_signals(
    identity: Dict[str, Any],
    client: anthropic.Anthropic,
    num_queries: int = DEFAULT_NUM_SEARCH_QUERIES,
    results_per_query: int = DEFAULT_RESULTS_PER_QUERY,
) -> Dict[str, Any]:
    """Run the targeted web searches. Returns the raw search corpus + queries."""
    name = identity["name"]
    company = identity["company"]

    queries = generate_search_queries(name, company, client, n=num_queries)
    try:
        search_results = search_web_batch(queries, num_results=results_per_query)
    except Exception as e:
        print(f"  web search batch failed: {e}", file=sys.stderr)
        search_results = []

    return {
        "search_queries": queries,
        "search_results": search_results,
    }


# ---------------------------------------------------------------------------
# Stage 3 + 4 — Extract & score
# ---------------------------------------------------------------------------

# Static instruction prefix — cached across every lead in a run (see _claude_json
# system=). Only the per-lead target + corpus go in the user message below.
EXTRACTION_SYSTEM = """You extract ONLY humanizing, conversational details about a person —
the kind of thing a friend would mention casually at dinner, NOT a CRM field.

GOOD signals:
- "Arsenal fan", "Big into F1", "Marathon runner", "Coffee nerd"
- "DJed in high school", "Plays Valorant", "Reads sci-fi"
- "Loves Interstellar", "Talks about cricket constantly"

BAD signals (REJECT these):
- "Raised funding", "CEO at company", "Hiring engineers"
- "Passionate about AI", "Building the future", "Startup operator"
- Any company achievements, professional accolades, or generic motivational fluff.

CRITICAL — IDENTITY VERIFICATION:
The internet is full of people with the same name. Before accepting ANY evidence,
you MUST verify the source is about the SAME person we're researching.
A source is acceptable ONLY if it does ONE of these:
  (a) Explicitly mentions the target company by name, OR
  (b) Explicitly references their LinkedIn URL, OR
  (c) Contains a biographical detail (role title, employer history, location)
      that is uniquely consistent with the person's LinkedIn profile.
If a source is just "<same first/last name> personal website" with NO mention
of the target company and NO biographical anchor matching the LinkedIn — REJECT IT.
A wrong-person signal is worse than no signal. When in doubt, reject.

DEDUP & CONSOLIDATION:
- Each signal must come from a UNIQUE source URL. Never cite the same URL twice.
- Each signal must be a DIFFERENT TOPIC. "Hard sciences nerd" and "physics
  background" are the same topic — pick the single strongest version.

OUTPUT — for each signal return:
- topic              (short label, max 8 words — e.g. "F1 fan", "Coffee nerd", "Plays Valorant")
- evidence_quote     (short verbatim quote — under 120 chars, trimmed)
- source_url         (the EXACT URL — must be unique across all signals)
- source_type        ("podcast" | "interview" | "blog" | "reddit" | "youtube" | "other")
- identity_anchor    (one short phrase explaining how you verified this source is the SAME
                      person — e.g. "page mentions <company>", "bio matches LinkedIn role")
- confidence         (0.0-1.0)
- small_talk_score   (0-10)

Hard rules for the returned list:
- Return AT MOST the requested number of signals.
- Every signal has a UNIQUE source_url. NEVER cite the same URL twice.
- Every signal covers a DIFFERENT FACT — not just a different label. If signal A's
  evidence quote already contains signal B's fact, collapse them into ONE richer signal.
- Each evidence_quote contains ONLY the fact for ITS signal. Trim it.
- Reject any signal whose identity_anchor is weak or absent.
- If genuinely humanizing AND identity-verified signals are fewer than requested,
  return fewer. NEVER pad. An empty array is a valid answer.

Return JSON: {"signals": [{...}, ...]}
Only valid JSON, no commentary."""


def extract_and_score_signals(
    identity: Dict[str, Any],
    harvest: Dict[str, Any],
    client: anthropic.Anthropic,
    max_signals: int = DEFAULT_MAX_SIGNALS,
) -> List[Dict[str, Any]]:
    name = identity["name"]
    company = identity["company"]

    # Build a compact web corpus for Claude.
    search_blocks = []
    for sr in harvest.get("search_results", []):
        for r in sr.get("results", [])[:DEFAULT_RESULTS_PER_QUERY]:
            search_blocks.append({
                "query": sr.get("query", ""),
                "url": r.get("url"),
                "title": r.get("title"),
                "summary": r.get("summary"),
                "highlights": r.get("highlights", []),
                "published": r.get("published_date"),
            })

    corpus = {"web": search_blocks[:30]}

    linkedin_url = identity.get("linkedin_url", "")

    prompt = f"""TARGET PERSON:
- Name:     {name}
- Company:  {company}
- LinkedIn: {linkedin_url or "(unknown)"}

Below is raw material gathered by web search. The internet is messy — many
results will be about other people who share this name. Apply the IDENTITY
VERIFICATION rules to every piece of evidence before keeping it. Reject anything
you can't anchor to {company} or to a biographical detail consistent with the
LinkedIn profile.

Return AT MOST {max_signals} signals.

Raw material (JSON):
{json.dumps(corpus, indent=2, ensure_ascii=False)[:14000]}"""

    try:
        out = _claude_json(client, prompt, max_tokens=2000, system=EXTRACTION_SYSTEM)
        signals = out.get("signals", []) if isinstance(out, dict) else []
    except Exception as e:
        print(f"  signal extraction failed: {e}", file=sys.stderr)
        return []

    # Final sort + truncate defensively
    signals.sort(key=lambda s: s.get("small_talk_score", 0), reverse=True)
    return signals[:max_signals]


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def format_small_talk_string(signals: List[Dict[str, Any]]) -> str:
    """Render top signals as compact 1-line bullets with source. Dedupes URLs."""
    lines = []
    seen_urls = set()
    seen_topics = set()
    for s in signals:
        topic = (s.get("topic") or "").strip()
        url = (s.get("source_url") or "").strip()
        if not topic:
            continue
        topic_key = topic.lower()
        if url and url in seen_urls:
            continue
        if topic_key in seen_topics:
            continue
        seen_urls.add(url)
        seen_topics.add(topic_key)

        src = (s.get("source_type") or "").strip()
        evidence = (s.get("evidence_quote") or "").strip().replace("\n", " ")
        if len(evidence) > 100:
            evidence = evidence[:100].rstrip() + "…"
        suffix_bits = []
        if evidence:
            suffix_bits.append(f'"{evidence}"')
        if url:
            suffix_bits.append(f"[{src}: {url}]" if src else f"[{url}]")
        suffix = " — " + " ".join(suffix_bits) if suffix_bits else ""
        lines.append(f"- {topic}{suffix}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Workflow-facing entrypoint
# ---------------------------------------------------------------------------

def scrape_small_talk(
    profile_url: str = "",
    name: str = "",
    company: str = "",
    website: str = "",
    max_signals: int = DEFAULT_MAX_SIGNALS,
    num_queries: int = DEFAULT_NUM_SEARCH_QUERIES,
) -> Dict[str, Any]:
    """
    Find humanizing small-talk signals for a person via precise web search.

    Args:
        profile_url:   LinkedIn URL (used only as an identity anchor, not scraped).
        name:          Full name.
        company:       Company.
        website:       Personal website if known (identity anchor).
        max_signals:   Max signals to return (default 3).
        num_queries:   Targeted web queries to generate (default 3).

    Returns:
        {
          "small_talk": "<2-3 line string with source>",
          "signals":    [{topic, evidence_quote, source_url, ...}, ...],
          "identity":   {name, company, linkedin_url, website},
        }
    """
    if not name:
        return {"small_talk": "", "signals": [], "identity": {}}

    client = anthropic.Anthropic()

    identity = build_identity(
        name=name, company=company, linkedin_url=profile_url, website=website,
    )

    harvest = harvest_signals(identity, client, num_queries=num_queries)

    signals = extract_and_score_signals(identity, harvest, client, max_signals=max_signals)

    return {
        "small_talk": format_small_talk_string(signals),
        "signals": signals,
        "identity": identity,
    }


if __name__ == "__main__":
    input_file = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(__file__), "example_input.json",
    )
    with open(input_file) as f:
        inp = json.load(f)

    result = scrape_small_talk(
        profile_url=inp.get("profile_url", ""),
        name=inp.get("name", ""),
        company=inp.get("company", ""),
        website=inp.get("website", ""),
        max_signals=inp.get("max_signals", DEFAULT_MAX_SIGNALS),
        num_queries=inp.get("num_queries", DEFAULT_NUM_SEARCH_QUERIES),
    )

    print(json.dumps(result, indent=2, ensure_ascii=False))
