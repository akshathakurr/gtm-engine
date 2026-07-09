"""
Blog Builder Workflow — research ideas, fill metadata, draft posts.

Modes:
  daily  — discover N blog ideas based on YOUR topics + reference companies, append to sheet
  idea   — pick up rows where Blog Idea is set + Keywords empty; research & fill metadata
  write  — take a researched row by number, write the full draft, save to a Google Doc

Requires three sections in `context/context.md`:
  ## Project              — what your project / company does (one paragraph)
  ## Blog Goals & Topics  — what audience / tone / topics you want blogs about
  ## Blog Reference Sources — companies whose blogs to model after (Name | domain)

If any of these is empty/missing, the workflow prompts you interactively at
the start of every run and (with your permission) appends your answers back
to context.md so you aren't re-asked. Use --auto to error out instead of
prompting.

Usage:
  python -m workflows.blog_builder.workflow --mode daily \\
      --sheet-id SHEET_ID --num-ideas 3
  python -m workflows.blog_builder.workflow --mode idea \\
      --sheet-id SHEET_ID
  python -m workflows.blog_builder.workflow --mode write \\
      --sheet-id SHEET_ID --row 2 --blogs-folder-id DRIVE_FOLDER_ID

Flags:
  --validate-keywords   sanity-check LLM-generated keywords against Google
                        Trends interest scores via scrapers/keyword_validator
  --auto                run non-interactively; abort if context.md is missing
                        any required section
"""

import os
import re
import sys
import json
import subprocess
import argparse
from datetime import datetime, timedelta
from typing import Optional, List, Dict

import anthropic
from exa_py import Exa

from config import CLAUDE_MODEL, CONTEXT_DIR, EXA_API_KEY
from workflows._common import (
    strip_json_fence as _strip_json_fence,
    TabularStore, find_col, ensure_col,
    read_context_file as _read_context_file,
    append_to_context_file as _append_to_context_file,
    section_body as _section_body,
)

try:
    from scrapers.keyword_validator.scraper import validate_keywords as _validate_keywords  # type: ignore
    _KW_VALIDATOR_AVAILABLE = True
except Exception:
    _validate_keywords = None  # type: ignore
    _KW_VALIDATOR_AVAILABLE = False


# ---------------------------------------------------------------------------
# Sheet schema (fixed — the workflow owns this)
# ---------------------------------------------------------------------------

SHEET_HEADERS = [
    "Blog Idea",        # A - 0
    "Why this Blog?",   # B - 1   (LLM rationale)
    "Project Name",     # C - 2   (optional — populated from CLI / left blank)
    "Reference",        # D - 3   (URLs of inspiration posts)
    "Talking Points",   # E - 4
    "Main Content",     # F - 5   (Google Doc URL after write mode)
    "SEO Target",       # G - 6
    "Keywords",         # H - 7
    "Keyword Score",    # I - 8   (only filled when --validate-keywords is used)
    "Assets",           # J - 9
    "Status",           # K - 10
    "Posting Date",     # L - 11
]
# Logical column name → the header it maps to. Indices are resolved at runtime
# from the store's ACTUAL header row (see resolve_columns), so a sheet with a
# different column order — or extra columns — is read and written correctly
# instead of silently misaligning.
_COL_NAMES = {
    "IDEA": "Blog Idea",      "WHY": "Why this Blog?",  "PROJECT": "Project Name",
    "REFERENCE": "Reference", "TALKING": "Talking Points", "MAIN": "Main Content",
    "SEO": "SEO Target",      "KEYWORDS": "Keywords",   "KW_SCORE": "Keyword Score",
    "ASSETS": "Assets",       "STATUS": "Status",       "DATE": "Posting Date",
}

STATUS_IDEA      = "Idea"
STATUS_DRAFT     = "Draft Ready"
STATUS_ASSET     = "Need Asset"
STATUS_LAUNCH    = "Ready to launch"
STATUS_PUBLISHED = "Published - live"
_TERMINAL_STATUSES = {STATUS_DRAFT.lower(), STATUS_ASSET.lower(),
                      STATUS_LAUNCH.lower(), STATUS_PUBLISHED.lower()}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def pad_row(row: List[str], n: int) -> List[str]:
    return list(row) + [""] * max(0, n - len(row))


def _to_str(v) -> str:
    if isinstance(v, list):
        return "\n".join(str(x) for x in v)
    return str(v) if v else ""


# ---------------------------------------------------------------------------
# Project context — load + parse + interactive backfill
# ---------------------------------------------------------------------------

# Filename inside CONTEXT_DIR where workflow writes user answers back.
_CONTEXT_FILE = "context.md"

REQUIRED_SECTIONS = [
    {
        "key": "project",
        "header": "Project",
        # The standard context.md (from the 2-question onboarding) describes the
        # project via Product + Who it's for, not a dedicated Project section.
        # Fall back to those so blog_builder works without a Project header.
        "fallback_headers": ["Product", "Who it's for"],
        "prompt": "What is your project / company? (one paragraph — what it does, who it's for, what problem it solves)",
    },
    {
        "key": "goals",
        "header": "Blog Goals & Topics",
        "prompt": "What blogs do you want? Who's the audience, what tone, what topics matter to your buyers? Be specific — the more concrete, the less generic the output.",
    },
    {
        "key": "references",
        "header": "Blog Reference Sources",
        "prompt": "Any companies whose blogs you'd like to model after? Format: 'Name | domain.com' per line. Press Enter to skip (we'll search broadly).",
        "multiline": True,
    },
]


def load_all_context() -> str:
    """Concatenate every .md in context/ (excluding *.example). For LLM grounding."""
    if not os.path.isdir(CONTEXT_DIR):
        return ""
    parts = []
    for fname in sorted(os.listdir(CONTEXT_DIR)):
        if fname.endswith(".md") and ".example" not in fname:
            with open(os.path.join(CONTEXT_DIR, fname)) as f:
                content = f.read().strip()
            if content:
                parts.append(f"=== {fname} ===\n{content}")
    return "\n\n".join(parts)


def parse_reference_sources(text: str) -> List[Dict[str, str]]:
    """
    Parse the 'Blog Reference Sources' section. Each non-empty line is
    'Name | domain.com'. Lines without '|' are treated as name-only.
    """
    body = _section_body(text, "Blog Reference Sources")
    out: List[Dict[str, str]] = []
    for line in body.splitlines():
        line = line.strip()
        # Skip blanks, comments, scaffolding bullets, and ``` code-fence lines
        # (the documented context.md wraps the reference block in a fence).
        if not line or line.startswith(("#", "(", "-", "```")):
            continue
        if "|" in line:
            name, domain = [x.strip() for x in line.split("|", 1)]
            out.append({"name": name, "domain": domain})
        else:
            out.append({"name": line, "domain": ""})
    return out


def _read_multiline_input(prompt_text: str) -> str:
    """Read multi-line user input until a blank line."""
    print(prompt_text)
    print("(End with an empty line.)")
    lines: List[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if not line.strip():
            break
        lines.append(line)
    return "\n".join(lines)


def ensure_context_complete(auto: bool) -> Dict[str, str]:
    """
    Walk REQUIRED_SECTIONS. For each missing/empty section:
      - if --auto, error out
      - else prompt the user, then offer to append their answer to context.md

    Returns {key: body} for all required sections (filled or freshly captured).
    """
    text = _read_context_file()
    captured: Dict[str, str] = {}
    missing: List[Dict[str, str]] = []

    for spec in REQUIRED_SECTIONS:
        body = _section_body(text, spec["header"])
        if not body:
            # Try fallback sections (e.g. Project ← Product + Who it's for)
            parts = [_section_body(text, h) for h in spec.get("fallback_headers", [])]
            body = "\n\n".join(p for p in parts if p).strip()
        if body:
            captured[spec["key"]] = body
        else:
            missing.append(spec)

    if not missing:
        return captured

    if auto:
        names = ", ".join(s["header"] for s in missing)
        print(f"\nERROR: --auto specified but context/context.md is missing required section(s): {names}")
        print("Add them to context/context.md and re-run, or drop --auto to be prompted.")
        sys.exit(2)

    print("\n" + "=" * 70)
    print(" Setup — your context.md is missing some sections we need")
    print("=" * 70)
    print("Answer the prompts below. With your permission we'll append the")
    print("answers to context/context.md so you aren't re-asked next run.\n")

    answers: Dict[str, str] = {}
    for spec in missing:
        print(f"[{spec['header']}]")
        print(spec["prompt"])
        if spec.get("multiline"):
            ans = _read_multiline_input("> ")
        else:
            ans = input("> ").strip()
        print()
        answers[spec["key"]] = ans
        captured[spec["key"]] = ans

    if any(v.strip() for v in answers.values()):
        choice = input("Save these answers to context/context.md? [Y/n] ").strip().lower()
        if choice in ("", "y", "yes"):
            for spec in missing:
                v = answers.get(spec["key"], "").strip()
                if v:
                    _append_to_context_file(spec["header"], v)
            print(f"Saved to {os.path.join(CONTEXT_DIR, _CONTEXT_FILE)}.\n")
        else:
            print("Not saved — the answers will be used for this run only.\n")

    return captured


# ---------------------------------------------------------------------------
# Row store helpers (Google Sheet or local CSV, via TabularStore)
# ---------------------------------------------------------------------------

def resolve_columns(store: TabularStore):
    """Resolve every logical column to its index in the store's ACTUAL header row
    (matched by name). Creates the schema if the store is empty, and appends any
    of the workflow's columns that are missing from an existing header — so reads
    and writes land in the right place regardless of the sheet's column order.

    Returns ``(cmap, width, rows)`` where ``cmap`` maps each `_COL_NAMES` key to
    its column index, ``width`` is the header width, and ``rows`` is the store's
    contents (header + data) after any header fix."""
    rows = store.read_all()
    if not rows:
        store.append([SHEET_HEADERS])
        headers = list(SHEET_HEADERS)
        rows = [headers]
    else:
        headers = list(rows[0])
        missing = [h for h in SHEET_HEADERS if find_col(headers, h) is None]
        if missing:
            for h in missing:
                ensure_col(headers, h)
            store.update_row(1, headers)
            print(f"  Added missing column(s) to {store.label()}: {missing}")
            rows = [headers] + rows[1:]
    cmap = {short: find_col(headers, name) for short, name in _COL_NAMES.items()}
    return cmap, len(headers), rows


# ---------------------------------------------------------------------------
# Exa research
# ---------------------------------------------------------------------------

def fetch_reference_posts(
    exa_client: Exa,
    references: List[Dict[str, str]],
    topic_hint: str,
    lookback_days: int = 90,
    num_per_company: int = 3,
) -> List[Dict]:
    posts: List[Dict] = []
    cutoff = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    for ref in references:
        domain = (ref.get("domain") or "").strip()
        name   = ref.get("name") or domain or "?"
        if not domain:
            print(f"  {name}: skipped (no domain)")
            continue
        try:
            results = exa_client.search(
                topic_hint,
                include_domains=[domain],
                num_results=num_per_company,
                type="fast",
                contents={"text": True, "highlights": True},
                start_published_date=cutoff,
            )
            for r in results.results:
                posts.append({
                    "company":        name,
                    "title":          r.title or "",
                    "url":            r.url or "",
                    "text":           (r.text or "")[:600],
                    "published_date": r.published_date or "",
                })
            print(f"  {name}: {len(results.results)} post(s)")
        except Exception as e:
            print(f"  {name}: skipped ({e})")

    return posts


def fetch_topic_posts(
    exa_client: Exa,
    queries: List[str],
    num_per_query: int = 5,
    lookback_days: int = 60,
) -> List[Dict]:
    posts: List[Dict] = []
    cutoff = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    for q in queries:
        try:
            results = exa_client.search(
                q,
                num_results=num_per_query,
                type="fast",
                contents={"text": True},
                start_published_date=cutoff,
            )
            for r in results.results:
                posts.append({
                    "source":         "topic",
                    "query":          q,
                    "title":          r.title or "",
                    "url":            r.url or "",
                    "text":           (r.text or "")[:500],
                    "published_date": r.published_date or "",
                })
        except Exception as e:
            print(f"  Topic '{q[:50]}': skipped ({e})")

    return posts


# ---------------------------------------------------------------------------
# Topic queries — derived from the user's "Blog Goals & Topics" section
# ---------------------------------------------------------------------------

def derive_topic_queries(
    project_text: str,
    goals_text: str,
    client: anthropic.Anthropic,
    max_queries: int = 5,
) -> List[str]:
    """
    Convert the user's free-text Blog Goals into concrete search queries.
    Falls back to splitting goals_text into lines if Claude is unavailable.
    """
    if not goals_text.strip():
        return []
    prompt = f"""Convert these blog goals into {max_queries} concrete web search queries that would surface recent articles relevant to the goals.

Project:
{project_text or "(not specified)"}

Blog Goals:
{goals_text}

Return a JSON array of {max_queries} query strings — each one a phrase someone would type into Google to find recent industry posts on these topics. No explanations.
Return only valid JSON."""
    try:
        resp = client.messages.create(
            model=CLAUDE_MODEL, max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        return json.loads(_strip_json_fence(resp.content[0].text))[:max_queries]
    except Exception as e:
        print(f"  Falling back from LLM topic derivation: {e}")
        # crude fallback — one query per non-empty line
        lines = [l.strip("-• ").strip() for l in goals_text.splitlines() if l.strip()]
        return lines[:max_queries]


# ---------------------------------------------------------------------------
# Claude prompts
# ---------------------------------------------------------------------------

def generate_daily_ideas(
    ref_posts: List[Dict],
    topic_posts: List[Dict],
    full_context: str,
    num_ideas: int,
    client: anthropic.Anthropic,
) -> List[Dict]:
    today = datetime.now().strftime("%Y-%m-%d")

    ref_block = "\n".join(
        f"[{p['company']}] {p['title']}\n{p['text'][:300]}\nURL: {p['url']}"
        for p in ref_posts[:40]
    ) or "(no reference posts available)"
    topic_block = "\n".join(
        f"[{p['query']}] {p['title']}\n{p['text'][:300]}\nURL: {p['url']}"
        for p in topic_posts[:20]
    ) or "(no recent topic posts found)"
    ctx_block = (
        f"=== PROJECT CONTEXT ===\n{full_context}"
        if full_context.strip()
        else "(No project context found — output may be generic.)"
    )

    prompt = f"""You are a B2B SaaS content strategist with deep SEO expertise.
Today: {today}

{ctx_block}

=== REFERENCE COMPANY BLOGS (recent posts — use for inspiration and gap analysis) ===
{ref_block}

=== RECENT POSTS ON THE USER'S TOPICS ===
{topic_block}

Task: Generate exactly {num_ideas} blog post ideas tailored to the project and goals above.

What makes a great blog:
- Targets keywords real buyers search (not vanity keywords)
- Teaches something genuinely useful — not marketing fluff
- Has a differentiated angle not already well-covered above
- Builds the company's credibility with their target audience

For each idea return:
- blog_idea:      a clear, compelling post title
- talking_points: array of 4-6 specific points to cover (concrete, not generic)
- keywords:       array of 5-8 SEO keywords (mix of head terms and long-tail buyer-intent queries)
- seo_target:    the ONE primary keyword this post should rank for
- assets:        what visuals/diagrams/screenshots would make this post better (1-2 ideas)
- posting_date:  suggested publish date (YYYY-MM-DD), space them ~1 week apart from {today}
- references:    array of up to 3 reference URLs above that inspired or are relevant
- why:           a SHORT 2-line rationale (max ~25 words total) — why this will rank AND who it's for. Punchy, no filler, no preamble.

Return a JSON array of exactly {num_ideas} objects with these exact keys.
Return only valid JSON, no explanation."""

    resp = client.messages.create(
        model=CLAUDE_MODEL, max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    return json.loads(_strip_json_fence(resp.content[0].text))


def research_specific_idea(
    idea: str,
    ref_posts: List[Dict],
    full_context: str,
    client: anthropic.Anthropic,
) -> Dict:
    today = datetime.now().strftime("%Y-%m-%d")

    ref_block = "\n".join(
        f"[{p.get('company', p.get('source', ''))}] {p['title']}\n{p['text'][:400]}\nURL: {p['url']}"
        for p in ref_posts[:25]
    ) or "(no reference posts available)"
    ctx_block = (
        f"=== PROJECT CONTEXT ===\n{full_context}"
        if full_context.strip()
        else "(No project context found — output may be generic.)"
    )

    prompt = f"""You are a B2B SaaS content strategist with deep SEO expertise.
Today: {today}

{ctx_block}

Blog idea to research: "{idea}"

=== RELATED REFERENCE POSTS ===
{ref_block}

Produce research metadata for this blog post:
- talking_points: array of 4-6 specific, concrete points to cover
- keywords:       array of 5-8 SEO keywords (mix of head + long-tail buyer-intent)
- seo_target:    the ONE primary keyword this post should rank for
- assets:        what visuals/diagrams/screenshots would strengthen the post (1-2 ideas)
- posting_date:  suggested publish date (YYYY-MM-DD), 1-2 weeks from today
- references:    array of up to 3 URLs from the reference posts above
- why:           a SHORT 2-line rationale (max ~25 words total) — why this will rank AND who it's for. Punchy, no filler, no preamble.

Return a single JSON object with these exact keys. Return only valid JSON, no explanation."""

    resp = client.messages.create(
        model=CLAUDE_MODEL, max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    return json.loads(_strip_json_fence(resp.content[0].text))


def write_blog_post(
    idea: str,
    talking_points: str,
    keywords: str,
    seo_target: str,
    full_context: str,
    client: anthropic.Anthropic,
) -> str:
    ctx_block = (
        f"=== PROJECT CONTEXT ===\n{full_context}"
        if full_context.strip()
        else "(No project context found — output may be generic.)"
    )

    prompt = f"""You are a B2B SaaS content writer. Write a complete, publish-ready blog post.

{ctx_block}

Blog title: {idea}
Primary SEO keyword: {seo_target}
All keywords to include naturally: {keywords}

Key points to cover:
{talking_points}

Requirements:
- 1200–1800 words
- Clear H2/H3 structure
- Intro that opens with a specific pain point or surprising insight (never "In today's fast-paced world")
- Include the primary SEO keyword in: the title, the first paragraph, at least one H2, and the conclusion
- Use concrete examples, real numbers, and specific scenarios
- Write like a smart founder explaining something valuable — no corporate-speak, no fluff
- End with a CTA that naturally connects to the project's product
- Format as clean markdown

Write the full blog post now."""

    resp = client.messages.create(
        model=CLAUDE_MODEL, max_tokens=6000,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text


# ---------------------------------------------------------------------------
# Keyword validation
# ---------------------------------------------------------------------------

def validate_idea_keywords(idea: Dict) -> Dict[str, int]:
    """Run the keyword_validator scraper on this idea's keywords. Returns {kw: score}."""
    if not _KW_VALIDATOR_AVAILABLE:
        print("  --validate-keywords requested but pytrends not installed — skipping.")
        return {}
    kws = idea.get("keywords") or []
    if not kws:
        return {}
    print(f"  Validating {len(kws)} keyword(s) via Google Trends...")
    result = _validate_keywords(keywords=kws, geo="", timeframe="today 12-m")
    return {kw: data.get("interest_score", 0) for kw, data in result.items()}


def format_keyword_scores(scores: Dict[str, int]) -> str:
    if not scores:
        return ""
    return "\n".join(f"{kw}: {s}/100" for kw, s in sorted(scores.items(), key=lambda x: -x[1]))


# ---------------------------------------------------------------------------
# Mode: daily
# ---------------------------------------------------------------------------

def run_daily(
    args: argparse.Namespace,
    client: anthropic.Anthropic,
    exa_client: Exa,
    store: TabularStore,
) -> None:
    print(f"\n=== Blog Builder | mode=daily | ideas={args.num_ideas} ===\n")

    sections     = ensure_context_complete(args.auto)
    full_context = load_all_context()
    references   = parse_reference_sources(_read_context_file())
    if args.reference_companies:
        # CLI override — comma-separated "Name|domain" entries
        references = []
        for entry in args.reference_companies.split(","):
            parts = [p.strip() for p in entry.split("|")]
            if len(parts) == 2:
                references.append({"name": parts[0], "domain": parts[1]})

    if not references:
        print("  No reference companies configured — proceeding without per-company filtering.")
    else:
        print(f"  Reference companies ({len(references)}): " +
              ", ".join(r["name"] for r in references))

    print("\n--- Deriving topic search queries from your goals ---")
    topic_queries = derive_topic_queries(
        sections.get("project", ""),
        sections.get("goals", ""),
        client,
    )
    if topic_queries:
        for q in topic_queries:
            print(f"  · {q}")
    else:
        print("  (none — Blog Goals & Topics empty; skipping topic search)")

    print("\n--- Fetching reference company posts ---")
    if references:
        # Use the user's goals as the topic hint if available
        topic_hint = (sections.get("goals") or "").strip().splitlines()
        topic_hint = topic_hint[0] if topic_hint else "B2B SaaS strategy"
        ref_posts = fetch_reference_posts(
            exa_client, references,
            topic_hint=topic_hint,
            lookback_days=90,
            num_per_company=3,
        )
    else:
        ref_posts = []
    print(f"Total reference posts: {len(ref_posts)}")

    print("\n--- Fetching topic-driven posts ---")
    topic_posts = fetch_topic_posts(exa_client, topic_queries) if topic_queries else []
    print(f"Total topic posts: {len(topic_posts)}")

    print("\n--- Generating ideas with Claude ---")
    ideas = generate_daily_ideas(ref_posts, topic_posts, full_context, args.num_ideas, client)
    print(f"Generated {len(ideas)} idea(s)")

    c, width, _ = resolve_columns(store)

    rows: List[List[str]] = []
    for idea in ideas:
        kw_scores: Dict[str, int] = {}
        if args.validate_keywords:
            kw_scores = validate_idea_keywords(idea)

        row = [""] * width
        row[c["IDEA"]]      = _to_str(idea.get("blog_idea", ""))
        row[c["WHY"]]       = _to_str(idea.get("why", ""))
        row[c["PROJECT"]]   = args.project_name or ""
        row[c["REFERENCE"]] = _to_str(idea.get("references", []))
        row[c["TALKING"]]   = "\n".join(f"• {pt}" for pt in (idea.get("talking_points") or []))
        row[c["MAIN"]]      = ""
        row[c["SEO"]]       = _to_str(idea.get("seo_target", ""))
        row[c["KEYWORDS"]]  = ", ".join(idea.get("keywords") or [])
        row[c["KW_SCORE"]]  = format_keyword_scores(kw_scores)
        row[c["ASSETS"]]    = _to_str(idea.get("assets", ""))
        row[c["STATUS"]]    = STATUS_IDEA
        row[c["DATE"]]      = _to_str(idea.get("posting_date", ""))
        rows.append(row)

    store.append(rows)

    print(f"\n✓ Added {len(rows)} row(s) to {store.label()}:")
    for r in rows:
        print(f"  • {r[c['IDEA']]}")
        print(f"    SEO target: {r[c['SEO']]}  |  Post date: {r[c['DATE']]}")


# ---------------------------------------------------------------------------
# Mode: idea
# ---------------------------------------------------------------------------

def run_idea(
    args: argparse.Namespace,
    client: anthropic.Anthropic,
    exa_client: Exa,
    store: TabularStore,
) -> None:
    print("\n=== Blog Builder | mode=idea ===\n")

    ensure_context_complete(args.auto)  # interactive backfill; idea mode reads full context below
    full_context = load_all_context()
    references   = parse_reference_sources(_read_context_file())
    if args.reference_companies:
        references = []
        for entry in args.reference_companies.split(","):
            parts = [p.strip() for p in entry.split("|")]
            if len(parts) == 2:
                references.append({"name": parts[0], "domain": parts[1]})

    c, width, all_rows = resolve_columns(store)
    data_rows = all_rows[1:]
    if not data_rows:
        print(f"{store.label()} is empty.")
        return

    pending: List[tuple] = []

    for i, row in enumerate(data_rows, start=2):
        r = pad_row(row, width)
        idea_text = r[c["IDEA"]].strip()
        keywords  = r[c["KEYWORDS"]].strip()
        status    = r[c["STATUS"]].strip().lower()

        if idea_text and not keywords and status not in _TERMINAL_STATUSES:
            pending.append((i, r, idea_text))

    if not pending:
        print("No pending rows (need: Blog Idea set, Keywords empty, Status not terminal).")
        return

    print(f"Found {len(pending)} pending row(s).\n")

    for row_idx, row_data, idea in pending:
        print(f"Row {row_idx}: '{idea}'")

        print("  Fetching research from references and topic searches...")
        ref_posts = (fetch_reference_posts(exa_client, references, topic_hint=idea,
                                           lookback_days=120, num_per_company=2)
                     if references else [])
        topic_posts = fetch_topic_posts(exa_client, [idea, f"{idea} B2B SaaS"], num_per_query=4)

        meta = research_specific_idea(idea, ref_posts + topic_posts, full_context, client)

        kw_scores: Dict[str, int] = {}
        if args.validate_keywords:
            kw_scores = validate_idea_keywords(meta)

        updated = list(row_data)
        updated[c["WHY"]]       = _to_str(meta.get("why", ""))
        updated[c["REFERENCE"]] = "\n".join(meta.get("references", []))
        updated[c["TALKING"]]   = "\n".join(f"• {pt}" for pt in meta.get("talking_points", []))
        updated[c["SEO"]]       = meta.get("seo_target", "")
        updated[c["KEYWORDS"]]  = ", ".join(meta.get("keywords", []))
        updated[c["KW_SCORE"]]  = format_keyword_scores(kw_scores)
        updated[c["ASSETS"]]    = _to_str(meta.get("assets", ""))
        updated[c["STATUS"]]    = STATUS_IDEA
        updated[c["DATE"]]      = meta.get("posting_date", "")
        if args.project_name and not updated[c["PROJECT"]].strip():
            updated[c["PROJECT"]] = args.project_name

        store.update_row(row_idx, updated)
        print(f"  ✓ Updated — SEO target: {meta.get('seo_target', '')}  |  Post date: {meta.get('posting_date', '')}\n")


# ---------------------------------------------------------------------------
# Mode: write
# ---------------------------------------------------------------------------

def create_blog_doc(
    blogs_folder_id: Optional[str],
    title: str,
    seo_target: str,
    keywords: str,
    content: str,
) -> str:
    """Create a Google Doc; if blogs_folder_id given, move it there. Returns edit URL."""
    result = subprocess.run(
        ["gws", "docs", "documents", "create",
         "--json", json.dumps({"title": title})],
        capture_output=True, text=True, check=True,
    )
    doc_id = json.loads(result.stdout)["documentId"]

    if blogs_folder_id:
        subprocess.run(
            ["gws", "drive", "files", "update",
             "--params", json.dumps({
                 "fileId": doc_id,
                 "addParents": blogs_folder_id,
                 "removeParents": "root",
             }),
             "--json", "{}"],
            capture_output=True, text=True, check=True,
        )

    full_text = (
        f"SEO Target: {seo_target}\n"
        f"Keywords: {keywords}\n"
        f"{'─' * 60}\n\n"
        f"{content}"
    )
    subprocess.run(
        ["gws", "docs", "documents", "batchUpdate",
         "--params", json.dumps({"documentId": doc_id}),
         "--json", json.dumps({
             "requests": [{
                 "insertText": {"location": {"index": 1}, "text": full_text}
             }]
         })],
        capture_output=True, text=True, check=True,
    )
    return f"https://docs.google.com/document/d/{doc_id}/edit"


def run_write(
    args: argparse.Namespace,
    client: anthropic.Anthropic,
    store: TabularStore,
) -> None:
    print(f"\n=== Blog Builder | mode=write | row={args.row} ===\n")

    ensure_context_complete(args.auto)
    full_context = load_all_context()

    c, width, all_rows = resolve_columns(store)
    if args.row < 2 or args.row > len(all_rows):
        print(f"Row {args.row} is out of range ({store.label()} has {len(all_rows)} rows).")
        return

    row         = pad_row(all_rows[args.row - 1], width)
    idea        = row[c["IDEA"]].strip()
    talking_pts = row[c["TALKING"]].strip()
    seo_target  = row[c["SEO"]].strip()
    keywords    = row[c["KEYWORDS"]].strip()

    if not idea:
        print("Row has no Blog Idea.")
        return

    print(f"Writing: '{idea}'")
    print(f"SEO target: {seo_target}\n")

    draft = write_blog_post(idea, talking_pts, keywords, seo_target, full_context, client)

    if store.is_csv:
        # No Google Doc without gws — write the draft to a local Markdown file
        # next to the CSV and record its path in Main Content.
        out_dir = os.path.dirname(os.path.abspath(store.csv_path)) or "."
        slug = re.sub(r"[^a-z0-9]+", "-", idea.lower()).strip("-")[:60] or f"row-{args.row}"
        md_path = os.path.join(out_dir, f"{slug}.md")
        with open(md_path, "w") as f:
            f.write(f"# {idea}\n\nSEO Target: {seo_target}\nKeywords: {keywords}\n\n---\n\n{draft}\n")
        main_content = md_path
        print(f"Draft written locally: {md_path}")
    else:
        print("Creating Google Doc...")
        main_content = create_blog_doc(args.blogs_folder_id, idea, seo_target, keywords, draft)
        print(f"Doc created: {main_content}")

    row[c["MAIN"]]   = main_content
    row[c["STATUS"]] = STATUS_DRAFT
    store.update_row(args.row, row)
    print(f"Updated — Status: Draft Ready, Main Content: {main_content}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Blog Builder Workflow")
    parser.add_argument("--mode", choices=["daily", "idea", "write"], required=True)
    parser.add_argument("--sheet-id",   default=None,
                        help="Google Sheet ID (from the URL). Or use --output-csv.")
    parser.add_argument("--sheet-name", default="Blogs",
                        help="Sheet tab name. Default: Blogs")
    parser.add_argument("--output-csv", default=None,
                        help="Track blogs in a local CSV instead of a Google Sheet. "
                             "In write mode the draft is saved as a local .md file.")
    parser.add_argument("--num-ideas",  type=int, default=3,
                        help="(daily mode) Number of ideas to generate. Default: 3")
    parser.add_argument("--row",        type=int, default=None,
                        help="(write mode) Sheet row number to draft.")
    parser.add_argument("--blogs-folder-id", default=None,
                        help="(write mode) Drive folder ID to place the new doc in. "
                             "If omitted, the doc is created in your root Drive.")
    parser.add_argument("--reference-companies", default=None,
                        help="Override Blog Reference Sources for this run only. "
                             "Format: 'Name1|domain1.com,Name2|domain2.com'")
    parser.add_argument("--project-name", default=None,
                        help="Project Name to write into column C of new rows "
                             "(useful when one sheet tracks blogs across multiple projects).")
    parser.add_argument("--validate-keywords", action="store_true",
                        help="Sanity-check LLM keywords against Google Trends "
                             "(requires pytrends).")
    parser.add_argument("--auto", action="store_true",
                        help="Run non-interactively. Errors out instead of prompting "
                             "if context.md is missing required sections.")
    args = parser.parse_args()

    if args.mode == "write" and not args.row:
        print("ERROR: --row is required for write mode")
        sys.exit(1)

    if bool(args.sheet_id) == bool(args.output_csv):
        print("ERROR: pass exactly one of --sheet-id or --output-csv")
        sys.exit(1)
    store = TabularStore(sheet_id=args.sheet_id, sheet_name=args.sheet_name,
                         csv_path=args.output_csv)

    client = anthropic.Anthropic()

    # Exa is only used to research ideas (daily/idea); write mode drafts an
    # existing row and needs no research, so don't require the key for it.
    if args.mode == "daily" or args.mode == "idea":
        if not EXA_API_KEY:
            print("Exa key missing — add EXA_API_KEY to .env to research blog ideas.")
            sys.exit(1)
        exa_client = Exa(api_key=EXA_API_KEY)
        if args.mode == "daily":
            run_daily(args, client, exa_client, store)
        else:
            run_idea(args, client, exa_client, store)
    elif args.mode == "write":
        run_write(args, client, store)


if __name__ == "__main__":
    main()
