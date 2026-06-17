"""
LinkedIn Comment Helper Workflow

Finds recent LinkedIn posts worth commenting on across 3 sources:
  1. ICP profiles  — read profile URLs from a Google Sheet
  2. Trending      — search broad genre keywords, re-rank by engagement
  3. Signal        — search buying-intent phrases (people implementing AI, etc.)

For each post, asks Claude (using the 'project context' skill) whether it's
worth commenting on and what angle the user has from past projects.

Appends new rows to a single rolling output sheet (deduped by Post URL).

Usage:
  python -m workflows.linkedin_comment_helper.workflow --mode auto
  python -m workflows.linkedin_comment_helper.workflow --mode interactive
"""

import os
import re
import sys
import json
import time
import argparse
import subprocess
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict

import anthropic

from config import CLAUDE_MODEL, CONTEXT_DIR
from scrapers.linkedin_profile_post_scraper import scraper as _post_scraper
from scrapers.linkedin_post_research import scraper as _search_scraper


# ---------------------------------------------------------------------------
# Inlined helpers (kept here so each workflow is self-contained)
# ---------------------------------------------------------------------------

def gws_read_sheet(sheet_id: str, sheet_name: str) -> List[List[str]]:
    """Return all rows from a sheet tab as a list of lists."""
    result = subprocess.run(
        ["gws", "sheets", "spreadsheets", "values", "get",
         "--params", json.dumps({"spreadsheetId": sheet_id, "range": sheet_name})],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout).get("values", [])


def gws_write_range(sheet_id: str, range_: str, values: List[List[str]]) -> None:
    """Write a list of rows to the given A1-notation range."""
    subprocess.run(
        ["gws", "sheets", "spreadsheets", "values", "update",
         "--params", json.dumps({"spreadsheetId": sheet_id, "range": range_,
                                  "valueInputOption": "USER_ENTERED"}),
         "--json", json.dumps({"values": values})],
        capture_output=True, text=True, check=True,
    )


def find_col(headers: List[str], *names: str) -> Optional[int]:
    lower = [n.lower() for n in names]
    for i, h in enumerate(headers):
        if h.strip().lower() in lower:
            return i
    return None


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

SLEEP_BETWEEN_PROFILES = 5  # apimaestro/linkedin-profile-posts throttles on bursts


# ---------------------------------------------------------------------------
# Context backfill — interactive pre-step for missing sections in context.md
# ---------------------------------------------------------------------------

_CONTEXT_FILE = "context.md"

REQUIRED_SECTIONS = [
    {
        "key": "project",
        "header": "Project",
        "prompt": "What is your project / company? (one paragraph — what it does, who it's for, what problem it solves)",
    },
    {
        "key": "genre_keywords",
        "header": "LinkedIn Comment Genre Keywords",
        "prompt": "What broad topics/genres do you want to comment on to build credibility? One per line (e.g. 'B2B SaaS', 'AI strategy', 'climate tech').",
        "multiline": True,
    },
    {
        "key": "signal_keywords",
        "header": "LinkedIn Comment Signal Keywords",
        "prompt": "What buying-intent / pain-point phrases should we hunt for? One per line (e.g. 'implementing AI in our org', 'switching CRMs').",
        "multiline": True,
    },
]


def _read_context_file() -> str:
    path = os.path.join(CONTEXT_DIR, _CONTEXT_FILE)
    if not os.path.exists(path):
        return ""
    with open(path) as f:
        return f.read()


def _append_to_context_file(section_header: str, body: str) -> None:
    path = os.path.join(CONTEXT_DIR, _CONTEXT_FILE)
    body = body.strip()
    if not body:
        return
    block = f"\n\n## {section_header}\n{body}\n"
    if os.path.exists(path):
        with open(path, "a") as f:
            f.write(block)
    else:
        with open(path, "w") as f:
            f.write("# Context\n" + block)


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
    return body


def _parse_keyword_lines(body: str) -> List[str]:
    """Each non-empty, non-comment line becomes a keyword."""
    out: List[str] = []
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("(") or line.startswith("-"):
            continue
        out.append(line)
    return out


def _read_multiline_input(prompt_text: str) -> str:
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
    """Walk REQUIRED_SECTIONS; prompt for missing ones; offer to save back."""
    text = _read_context_file()
    captured: Dict[str, str] = {}
    missing: List[Dict[str, str]] = []

    for spec in REQUIRED_SECTIONS:
        body = _section_body(text, spec["header"])
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

try:
    from skills.project_context import skill as _project_context_skill  # type: ignore
    _PROJECT_CONTEXT_AVAILABLE = True
except Exception:
    _project_context_skill = None  # type: ignore
    _PROJECT_CONTEXT_AVAILABLE = False


# ---------------------------------------------------------------------------
# Normalization — different scrapers have slightly different field names
# ---------------------------------------------------------------------------

def _normalize_profile_post(post: dict, source: str) -> dict:
    author = post.get("author") or {}
    stats = post.get("stats") or {}
    return {
        "post_url": post.get("url", "") or "",
        "post_id": post.get("urn", "") or "",
        "text": post.get("text", "") or "",
        "author_name": author.get("name", "") or "",
        "author_headline": author.get("headline", "") or "",
        "author_profile": author.get("profile_url", "") or "",
        "posted_at": post.get("posted_at", "") or "",
        "timestamp_ms": post.get("timestamp_ms"),
        "reactions": stats.get("total_reactions", 0) or 0,
        "comments": stats.get("comments", 0) or 0,
        "source": source,
    }


def _normalize_search_post(post: dict, source: str) -> dict:
    author = post.get("author") or {}
    stats = post.get("stats") or {}
    return {
        "post_url": post.get("post_url", "") or "",
        "post_id": post.get("post_id", "") or "",
        "text": post.get("text", "") or "",
        "author_name": author.get("name", "") or "",
        "author_headline": author.get("headline", "") or "",
        "author_profile": author.get("profile_url", "") or "",
        "posted_at": post.get("posted_at", "") or "",
        "timestamp_ms": post.get("posted_at_timestamp"),
        "reactions": stats.get("reactions", 0) or 0,
        "comments": stats.get("comments", 0) or 0,
        "source": source,
    }


# ---------------------------------------------------------------------------
# Source pulls
# ---------------------------------------------------------------------------

def pull_icp_posts(profile_urls: List[str], max_per_profile: int, days_back: int) -> List[dict]:
    posts: List[dict] = []
    for i, url in enumerate(profile_urls):
        if i > 0:
            time.sleep(SLEEP_BETWEEN_PROFILES)
        try:
            result = _post_scraper.scrape_linkedin_profile_posts(
                profile_url=url,
                max_posts=max_per_profile,
                days_back=days_back,
            )
            posts.extend(_normalize_profile_post(p, "icp") for p in result.get("posts", []))
        except Exception as e:
            print(f"  ICP profile {url} failed: {e}")
    return posts


def pull_trending_posts(keywords: List[str], max_per_keyword: int, days_back: int) -> List[dict]:
    """Search by genre keywords (relevance sort), filter to recent, re-rank by engagement."""
    results = _search_scraper.search_linkedin_posts_batch(
        keywords=keywords, sort="relevance", max_posts=max_per_keyword,
    )
    posts: List[dict] = []
    for r in results:
        for p in r.get("posts", []):
            posts.append(_normalize_search_post(p, "trending"))

    cutoff_ms = (datetime.now(timezone.utc) - timedelta(days=days_back)).timestamp() * 1000
    posts = [p for p in posts if p.get("timestamp_ms") and p["timestamp_ms"] >= cutoff_ms]

    posts.sort(key=lambda p: p["reactions"] + p["comments"], reverse=True)
    return posts


def pull_signal_posts(keywords: List[str], max_per_keyword: int, days_back: int) -> List[dict]:
    """Search by signal keywords (date sort), filter to recent."""
    results = _search_scraper.search_linkedin_posts_batch(
        keywords=keywords, sort="date_posted", max_posts=max_per_keyword,
    )
    posts: List[dict] = []
    for r in results:
        for p in r.get("posts", []):
            posts.append(_normalize_search_post(p, "signal"))

    cutoff_ms = (datetime.now(timezone.utc) - timedelta(days=days_back)).timestamp() * 1000
    posts = [p for p in posts if p.get("timestamp_ms") and p["timestamp_ms"] >= cutoff_ms]
    return posts


# ---------------------------------------------------------------------------
# Dedupe
# ---------------------------------------------------------------------------

def dedupe_posts(posts: List[dict]) -> List[dict]:
    seen = set()
    out: List[dict] = []
    for p in posts:
        pid = p.get("post_id") or p.get("post_url")
        if pid and pid not in seen:
            seen.add(pid)
            out.append(p)
    return out


# ---------------------------------------------------------------------------
# Relevance scoring (Claude + project context skill)
# ---------------------------------------------------------------------------

def _strip_json_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1]).strip()
    return text


def score_relevance(posts: List[dict], project_context: str, client: anthropic.Anthropic) -> List[Optional[dict]]:
    if not posts:
        return []

    posts_block = "\n\n".join(
        f"[{i+1}] Author: {p['author_name']} ({p['author_headline']})\n"
        f"Source: {p['source']}\n"
        f"Posted: {p['posted_at']}\n"
        f"Engagement: {p['reactions']} reactions, {p['comments']} comments\n"
        f"Text: {(p['text'] or '')[:600]}"
        for i, p in enumerate(posts)
    )

    prompt = f"""You are helping me decide which LinkedIn posts to comment on to build credibility in my space. Use my project context below to infer my genre, audience, and what kinds of posts are relevant — do NOT assume any specific industry.

My project context (everything I have worked on, and what my project is about):
{project_context}

For each post below, answer:
- worth_commenting: true/false. True if the post is substantive and a thoughtful comment would add value. False for vanity, generic platitudes, or pure self-promotion.
- relevance: ONE short sentence — why is this post relevant to my work or audience?
- angle: ONE short sentence — what specific hook from my past projects gives me something credible to say? If no specific project applies, write "general credibility — <how I'd add value, e.g. ask a sharp question about X>".

Posts:
{posts_block}

Return a JSON array — one object per post — with keys: index (1-based), worth_commenting, relevance, angle.
Return only valid JSON, no explanation."""

    resp = client.messages.create(
        model=CLAUDE_MODEL, max_tokens=8000,
        messages=[{"role": "user", "content": prompt}],
    )
    parsed = json.loads(_strip_json_fence(resp.content[0].text))
    result: List[Optional[dict]] = [None] * len(posts)
    for item in parsed:
        i = item.get("index", 0) - 1
        if 0 <= i < len(posts):
            result[i] = item
    return result


# ---------------------------------------------------------------------------
# Sheet output (rolling, append-only, deduped by URL)
# ---------------------------------------------------------------------------

SHEET_HEADERS = [
    "Run Date", "Post URL", "Author", "Source", "Posted",
    "Why relevant", "My angle", "Reactions", "Comments", "Status",
]


def read_icp_profile_urls(sheet_id: str, sheet_name: str, url_column: str) -> List[str]:
    rows = gws_read_sheet(sheet_id, sheet_name)
    if not rows:
        return []
    headers = rows[0]
    idx = find_col(headers, url_column)
    if idx is None:
        print(f"  Column '{url_column}' not found in headers: {headers}")
        return []
    return [cell(r, idx) for r in rows[1:] if cell(r, idx).startswith("http")]


def append_to_sheet(sheet_id: str, sheet_name: str, posts: List[dict], scores: List[Optional[dict]]) -> int:
    existing = gws_read_sheet(sheet_id, sheet_name) or []
    is_new_sheet = not existing
    headers = existing[0] if existing else SHEET_HEADERS

    # Schema-divergence guard: don't rewrite headers if data rows exist under a
    # different schema. Warn and append in workflow's column order — caller
    # should reconcile manually if alignment matters.
    if not is_new_sheet and headers != SHEET_HEADERS and len(existing) > 1:
        print(f"  Note: '{sheet_name}' has data rows under a different header schema. "
              f"Keeping existing header; new rows append in workflow's column order "
              f"(may not align with existing labels).")

    existing_urls: set = set()
    if not is_new_sheet:
        url_idx = find_col(headers, "Post URL")
        if url_idx is not None:
            for row in existing[1:]:
                u = cell(row, url_idx)
                if u:
                    existing_urls.add(u)

    run_date = datetime.now().strftime("%Y-%m-%d")
    new_rows: List[List[str]] = []
    for p, s in zip(posts, scores):
        if not p["post_url"] or p["post_url"] in existing_urls:
            continue
        if not s or not s.get("worth_commenting"):
            continue
        new_rows.append([
            run_date,
            p["post_url"],
            p["author_name"],
            p["source"],
            p["posted_at"],
            s.get("relevance", ""),
            s.get("angle", ""),
            str(p["reactions"]),
            str(p["comments"]),
            "",
        ])

    if not new_rows:
        return 0

    if is_new_sheet:
        gws_write_range(sheet_id, f"{sheet_name}!A1", [headers] + new_rows)
    else:
        start_row = len(existing) + 1
        gws_write_range(sheet_id, f"{sheet_name}!A{start_row}", new_rows)
    return len(new_rows)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _pause(message: str) -> None:
    input(f"\n[INTERACTIVE] {message} — press Enter to continue, Ctrl+C to abort.\n")


def _load_keywords(workflow_dir: str, filename: str, ctx_section: Optional[str] = None) -> List[str]:
    """
    Resolution order:
      1. context.md `## {ctx_section}` (one keyword per line)  ← new, preferred
      2. /Context/<project>/<filename>.json (legacy per-project override)
      3. workflow_dir/<filename>.json (default, kept for backward compat)
    """
    if ctx_section:
        body = _section_body(_read_context_file(), ctx_section)
        kws = _parse_keyword_lines(body)
        if kws:
            return kws

    context_path = os.path.join(CONTEXT_DIR, filename)
    fallback_path = os.path.join(workflow_dir, filename)
    path = context_path if os.path.exists(context_path) else fallback_path
    with open(path) as f:
        return json.load(f)["keywords"]


def _load_project_context() -> str:
    if _PROJECT_CONTEXT_AVAILABLE:
        return _project_context_skill.get_context()  # type: ignore
    print("  Note: 'project context' skill failed to import — falling back to raw context.md concat.")
    try:
        return load_icp()
    except Exception:
        return "(no project context available)"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["auto", "interactive"], default="auto")
    parser.add_argument("--icp-sheet-id")
    parser.add_argument("--icp-sheet-name")
    parser.add_argument("--icp-url-column")
    parser.add_argument("--output-sheet-id")
    parser.add_argument("--output-sheet-name")
    parser.add_argument("--days-back", type=int)
    parser.add_argument("--max-per-profile", type=int)
    parser.add_argument("--max-per-keyword", type=int)
    parser.add_argument("--skip-icp", action="store_true")
    parser.add_argument("--skip-trending", action="store_true")
    parser.add_argument("--skip-signal", action="store_true")
    parser.add_argument("--auto", action="store_true",
                        help="Run non-interactively. Errors out if context.md is missing required sections.")
    args = parser.parse_args()

    # Pre-step: backfill any missing context sections before doing any work.
    ensure_context_complete(auto=args.auto)

    workflow_dir = os.path.dirname(os.path.abspath(__file__))

    with open(os.path.join(workflow_dir, "config.json")) as f:
        cfg = json.load(f)

    icp_sheet_id    = args.icp_sheet_id    or cfg["icp_sheet"]["id"]
    icp_sheet_name  = args.icp_sheet_name  or cfg["icp_sheet"]["tab"]
    icp_url_col     = args.icp_url_column  or cfg["icp_sheet"]["url_column"]
    out_sheet_id    = args.output_sheet_id or cfg["output_sheet"]["id"]
    out_sheet_name  = args.output_sheet_name or cfg["output_sheet"]["tab"]
    days_back       = args.days_back       or cfg["days_back"]
    max_per_profile = args.max_per_profile or cfg["max_per_profile"]
    max_per_keyword = args.max_per_keyword or cfg["max_per_keyword"]
    trending_top_n  = cfg.get("trending_top_n", 30)

    genre_keywords = _load_keywords(workflow_dir, "genre_keywords.json",
                                    ctx_section="LinkedIn Comment Genre Keywords")
    signal_keywords = _load_keywords(workflow_dir, "signal_keywords.json",
                                     ctx_section="LinkedIn Comment Signal Keywords")

    interactive = args.mode == "interactive"
    print(f"Mode: {args.mode} | Days back: {days_back}")

    all_posts: List[dict] = []

    # ---- Source 1: ICP profiles
    if not args.skip_icp:
        if "TODO" in icp_sheet_id:
            print("[ICP] Sheet ID not configured in config.json — skipping ICP source.")
        else:
            print(f"[ICP] Reading profile URLs from sheet '{icp_sheet_name}'...")
            urls = read_icp_profile_urls(icp_sheet_id, icp_sheet_name, icp_url_col)
            print(f"  Found {len(urls)} ICP profile URLs.")
            if urls:
                print(f"[ICP] Pulling posts ({SLEEP_BETWEEN_PROFILES}s between profiles)...")
                icp_posts = pull_icp_posts(urls, max_per_profile, days_back)
                print(f"  Got {len(icp_posts)} ICP posts.")
                all_posts.extend(icp_posts)
        if interactive:
            _pause("ICP pull complete")

    # ---- Source 2: Trending
    if not args.skip_trending:
        print(f"[TRENDING] Searching {len(genre_keywords)} genre keywords...")
        trending = pull_trending_posts(genre_keywords, max_per_keyword, days_back)
        trending = trending[:trending_top_n]
        print(f"  Got {len(trending)} trending posts (top {trending_top_n} by engagement).")
        all_posts.extend(trending)
        if interactive:
            _pause("Trending pull complete")

    # ---- Source 3: Signal
    if not args.skip_signal:
        print(f"[SIGNAL] Searching {len(signal_keywords)} signal keywords...")
        signal_posts = pull_signal_posts(signal_keywords, max_per_keyword, days_back)
        print(f"  Got {len(signal_posts)} signal posts.")
        all_posts.extend(signal_posts)
        if interactive:
            _pause("Signal pull complete")

    # ---- Dedupe
    all_posts = dedupe_posts(all_posts)
    print(f"\n[DEDUPE] {len(all_posts)} unique posts across all sources.")

    # Save raw locally
    with open(os.path.join(workflow_dir, "results.json"), "w") as f:
        json.dump({"run_date": datetime.now().isoformat(), "posts": all_posts}, f, indent=2, ensure_ascii=False)

    if not all_posts:
        print("No posts to score. Exiting.")
        return

    # ---- Score relevance
    print(f"\n[SCORE] Scoring {len(all_posts)} posts via Claude...")
    project_context = _load_project_context()
    client = anthropic.Anthropic()
    scores = score_relevance(all_posts, project_context, client)
    worth = sum(1 for s in scores if s and s.get("worth_commenting"))
    print(f"  {worth} of {len(all_posts)} posts marked worth commenting.")

    # Save scores alongside posts
    with open(os.path.join(workflow_dir, "results.json"), "w") as f:
        json.dump({
            "run_date": datetime.now().isoformat(),
            "posts": all_posts,
            "scores": scores,
        }, f, indent=2, ensure_ascii=False)

    if interactive:
        _pause("Scoring complete — review results.json before sheet write")

    # ---- Append to sheet
    if "TODO" in out_sheet_id:
        print("\n[OUTPUT] Output sheet ID not configured. Saved to results.json only.")
        return

    print(f"\n[OUTPUT] Appending to sheet '{out_sheet_name}'...")
    n = append_to_sheet(out_sheet_id, out_sheet_name, all_posts, scores)
    print(f"  Appended {n} new rows.")


if __name__ == "__main__":
    main()
