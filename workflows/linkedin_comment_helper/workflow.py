"""
LinkedIn Comment Helper Workflow

Finds recent LinkedIn posts worth commenting on across 3 sources:
  1. ICP profiles  — read profile URLs from a Google Sheet
  2. Trending      — search broad genre keywords, re-rank by engagement
  3. Signal        — search buying-intent phrases (people implementing AI, etc.)

For each post, asks Claude (using the project context loaded from the context/
folder) whether it's worth commenting on and what angle the user has from past
projects.

Appends new rows to a single rolling output sheet (deduped by Post URL).

Usage:
  python -m workflows.linkedin_comment_helper.workflow --mode auto
  python -m workflows.linkedin_comment_helper.workflow --mode interactive
"""

import os
import sys
import json
import time
import argparse
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict

import anthropic

from config import CLAUDE_MODEL, CONTEXT_DIR
from workflows._common import (
    strip_json_fence as _strip_json_fence,
    gws_read_sheet, gws_write_range, find_col, cell, load_icp,
    read_context_file as _read_context_file,
    append_to_context_file as _append_to_context_file,
    section_body as _section_body,
    preview_and_confirm,
)
from scrapers.linkedin_profile_post_scraper import scraper as _post_scraper
from scrapers.linkedin_post_research import scraper as _search_scraper


SLEEP_BETWEEN_PROFILES = 5  # apimaestro/linkedin-profile-posts throttles on bursts
SEARCH_MIN_INTERVAL = 2     # spacing between posts-search actor calls (was the batch sleep)
ICP_CONCURRENCY = 3         # max in-flight profile-posts runs (bounded to stay well under free-plan limits)
SEARCH_CONCURRENCY = 3      # max in-flight posts-search runs


# ---------------------------------------------------------------------------
# Rate-limited concurrency
#
# Apify actors throttle on bursts, so we space request *starts* by a minimum
# interval (the same intervals the old serial sleeps used) while letting the
# slow actor run-times overlap across a small, bounded pool. In-flight runs are
# capped by max_workers, so we never exceed the free plan's concurrent-run room.
# ---------------------------------------------------------------------------

import threading
from concurrent.futures import ThreadPoolExecutor


class _RateLimiter:
    """Spaces successive acquire() calls >= min_interval apart (thread-safe).

    Spacing applies to call *starts*, so work done after acquire() overlaps
    across threads without ever bursting the actor.
    """

    def __init__(self, min_interval: float):
        self._min_interval = min_interval
        self._lock = threading.Lock()
        self._next = 0.0

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._next - now
            if wait > 0:
                time.sleep(wait)
                now += wait
            self._next = now + self._min_interval


def _map_rate_limited(fn, items: list, *, min_interval: float, max_workers: int):
    """Run fn(item) over a bounded thread pool, starts spaced >= min_interval.

    Returns (results, errors) — two lists aligned to `items`. fn exceptions are
    captured (results[i] is None, errors[i] is the exception), never raised, so
    one bad item can't kill the batch.
    """
    n = len(items)
    if n == 0:
        return [], []
    limiter = _RateLimiter(min_interval)
    results: List[Optional[object]] = [None] * n
    errors: List[Optional[Exception]] = [None] * n

    def task(i: int, item):
        limiter.acquire()
        try:
            return i, fn(item), None
        except Exception as e:  # noqa: BLE001 — captured per-item, surfaced to caller
            return i, None, e

    with ThreadPoolExecutor(max_workers=min(max_workers, n)) as ex:
        for fut in [ex.submit(task, i, it) for i, it in enumerate(items)]:
            i, res, err = fut.result()
            results[i] = res
            errors[i] = err
    return results, errors


# ---------------------------------------------------------------------------
# Context backfill — interactive pre-step for missing sections in context.md
# ---------------------------------------------------------------------------

_CONTEXT_FILE = "context.md"

REQUIRED_SECTIONS = [
    {
        "key": "project",
        "header": "Project",
        # The standard context.md describes the project via Product + Who it's
        # for, not a dedicated Project section. Fall back to those.
        "fallback_headers": ["Product", "Who it's for"],
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
        if not body:
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
    # One profile-posts run per URL, starts spaced SLEEP_BETWEEN_PROFILES apart,
    # run-times overlapped across a small pool.
    def scrape_one(url: str) -> dict:
        return _post_scraper.scrape_linkedin_profile_posts(
            profile_url=url,
            max_posts=max_per_profile,
            days_back=days_back,
        )

    results, errors = _map_rate_limited(
        scrape_one, profile_urls,
        min_interval=SLEEP_BETWEEN_PROFILES, max_workers=ICP_CONCURRENCY,
    )
    posts: List[dict] = []
    for url, res, err in zip(profile_urls, results, errors):
        if err:
            print(f"  ICP profile {url} failed: {err}")
            continue
        posts.extend(_normalize_profile_post(p, "icp") for p in (res or {}).get("posts", []))
    return posts


def _search_keywords(keywords: List[str], sort: str, max_posts: int) -> List[dict]:
    """Run posts-search for each keyword concurrently (starts spaced), in order."""
    def search_one(kw: str) -> dict:
        return _search_scraper.search_linkedin_posts(kw, sort=sort, max_posts=max_posts)

    results, errors = _map_rate_limited(
        search_one, keywords,
        min_interval=SEARCH_MIN_INTERVAL, max_workers=SEARCH_CONCURRENCY,
    )
    for kw, err in zip(keywords, errors):
        if err:
            print(f"  Search keyword '{kw}' failed: {err}")
    return [r for r in results if r]


def pull_trending_posts(keywords: List[str], max_per_keyword: int, days_back: int) -> List[dict]:
    """Search by genre keywords (relevance sort), filter to recent, re-rank by engagement."""
    results = _search_keywords(keywords, sort="relevance", max_posts=max_per_keyword)
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
    results = _search_keywords(keywords, sort="date_posted", max_posts=max_per_keyword)
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
# Relevance scoring (Claude + project context from the context/ folder)
# ---------------------------------------------------------------------------

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
    try:
        parsed = json.loads(_strip_json_fence(resp.content[0].text))
    except json.JSONDecodeError:
        # One malformed reply shouldn't discard every scraped post; score nothing
        # this run and let the caller report zero matches.
        print("  ! Couldn't parse the relevance scores from Claude — skipping scoring this run.")
        return [None] * len(posts)
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
    """Project context lives in the context/ folder — load every .md file in it."""
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

    # ICP profile URLs are read up front so the spend estimate below can size
    # the (expensive) profile-posts fan-out before anything is billed.
    icp_urls: List[str] = []
    if not args.skip_icp:
        if "TODO" in icp_sheet_id:
            print("[ICP] Sheet ID not configured in config.json — skipping ICP source.")
        else:
            print(f"[ICP] Reading profile URLs from sheet '{icp_sheet_name}'...")
            raw_urls = read_icp_profile_urls(icp_sheet_id, icp_sheet_name, icp_url_col)
            # Dedupe — duplicate URLs would otherwise pay for the same run twice.
            icp_urls = list(dict.fromkeys(raw_urls))
            dropped = len(raw_urls) - len(icp_urls)
            note = f" ({dropped} duplicate(s) skipped)" if dropped else ""
            print(f"  Found {len(icp_urls)} ICP profile URLs{note}.")

    # ---- Spend preview. Worst case = every requested item returned + billed.
    if not preview_and_confirm([
        ("ICP profile posts",  "linkedin_profile_posts", len(icp_urls) * max_per_profile),
        ("Trending post search", "linkedin_post_search",
         (0 if args.skip_trending else len(genre_keywords) * max_per_keyword)),
        ("Signal post search",   "linkedin_post_search",
         (0 if args.skip_signal else len(signal_keywords) * max_per_keyword)),
    ], interactive=interactive):
        print("Aborted — no Apify runs were started.")
        return

    # ---- Sources. ICP uses the profile-posts actor; trending + signal share
    # the posts-search actor. Each source below handles its own skip/config and
    # returns its posts. In auto mode the ICP stream and the search stream run
    # concurrently (different actors → no shared throttle); within each stream
    # calls stay rate-limited. Interactive mode runs them sequentially so the
    # between-source pauses still make sense.

    def run_icp() -> List[dict]:
        if not icp_urls:
            return []
        print(f"[ICP] Pulling posts (≥{SLEEP_BETWEEN_PROFILES}s between starts, up to {ICP_CONCURRENCY} in flight)...")
        icp_posts = pull_icp_posts(icp_urls, max_per_profile, days_back)
        print(f"  Got {len(icp_posts)} ICP posts.")
        return icp_posts

    def run_trending() -> List[dict]:
        if args.skip_trending:
            return []
        print(f"[TRENDING] Searching {len(genre_keywords)} genre keywords...")
        trending = pull_trending_posts(genre_keywords, max_per_keyword, days_back)[:trending_top_n]
        print(f"  Got {len(trending)} trending posts (top {trending_top_n} by engagement).")
        return trending

    def run_signal() -> List[dict]:
        if args.skip_signal:
            return []
        print(f"[SIGNAL] Searching {len(signal_keywords)} signal keywords...")
        signal_posts = pull_signal_posts(signal_keywords, max_per_keyword, days_back)
        print(f"  Got {len(signal_posts)} signal posts.")
        return signal_posts

    if interactive:
        all_posts.extend(run_icp())
        if not args.skip_icp:
            _pause("ICP pull complete")
        all_posts.extend(run_trending())
        if not args.skip_trending:
            _pause("Trending pull complete")
        all_posts.extend(run_signal())
        if not args.skip_signal:
            _pause("Signal pull complete")
    else:
        # Search stream keeps trending → signal sequential (one stream on the
        # shared posts-search actor), running alongside the ICP stream.
        def search_stream() -> List[dict]:
            return run_trending() + run_signal()

        with ThreadPoolExecutor(max_workers=2) as ex:
            icp_fut = ex.submit(run_icp)
            search_fut = ex.submit(search_stream)
            all_posts.extend(icp_fut.result())
            all_posts.extend(search_fut.result())

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
