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
import argparse
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict

import anthropic

from config import CLAUDE_MODEL, CONTEXT_DIR
from workflows._common import (
    strip_json_fence as _strip_json_fence,
    find_col, cell, load_icp,
    read_context_file as _read_context_file,
    append_to_context_file as _append_to_context_file,
    section_body as _section_body,
    preview_and_confirm, TabularStore,
    map_rate_limited as _map_rate_limited,
    checkpoint_path, checkpoint_load, checkpoint_append,
    estimate_apify_cost,
    CONTEXT_FILE as _CONTEXT_FILE,
)
from scrapers.linkedin_profile_post_scraper import scraper as _post_scraper
from scrapers.linkedin_post_research import scraper as _search_scraper


SLEEP_BETWEEN_PROFILES = 5  # apimaestro/linkedin-profile-posts throttles on bursts
SEARCH_MIN_INTERVAL = 2     # spacing between posts-search actor calls (was the batch sleep)
ICP_CONCURRENCY = 3         # max in-flight profile-posts runs (bounded to stay well under free-plan limits)
SEARCH_CONCURRENCY = 3      # max in-flight posts-search runs
VELOCITY_MIN_HOURS = 2.0    # age floor for velocity so just-posted noise can't dominate the ranking


from concurrent.futures import ThreadPoolExecutor


# ---------------------------------------------------------------------------
# Context backfill — interactive pre-step for missing sections in context.md
# ---------------------------------------------------------------------------

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
        "intents": {"engagement"},  # only the reach play searches genre keywords
    },
    {
        "key": "signal_keywords",
        "header": "LinkedIn Comment Signal Keywords",
        "prompt": "What buying-intent / pain-point phrases should we hunt for? One per line (e.g. 'implementing AI in our org', 'switching CRMs').",
        "multiline": True,
        "intents": {"prospect"},  # only the prospect play searches signal phrases
    },
]


def _parse_keyword_lines(body: str) -> List[str]:
    """Each non-empty, non-comment line becomes a keyword (markdown bullets OK)."""
    out: List[str] = []
    for line in body.splitlines():
        line = line.strip()
        if line.startswith(("- ", "* ")):
            line = line[2:].strip()
        # Skip blanks, comments, placeholders, and markdown horizontal rules
        # ("---" separators are included in section bodies).
        if not line or line.startswith("#") or line.startswith("(") or set(line) <= set("-*_"):
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


def ensure_context_complete(auto: bool, intent: Optional[str] = None) -> Dict[str, str]:
    """Walk REQUIRED_SECTIONS; prompt for missing ones; offer to save back.

    Sections tagged with an `intents` set are only required when the current run's
    intent is in it — so a prospect run never asks for genre keywords, and vice versa.
    """
    text = _read_context_file()
    captured: Dict[str, str] = {}
    missing: List[Dict[str, str]] = []

    for spec in REQUIRED_SECTIONS:
        wanted = spec.get("intents")
        if wanted and intent is not None and intent not in wanted:
            continue  # this section isn't used by the chosen intent
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

def pull_profile_posts(profile_urls: List[str], max_per_profile: int, days_back: int,
                       source: str, ck_path: Optional[str] = None) -> List[dict]:
    """Pull recent posts from a list of profile URLs, tagged with `source`.

    Used for both the prospect (ICP) list and the authority-accounts list — same
    actor, different `source` tag so downstream can tell the two plays apart.
    One run per URL, starts spaced SLEEP_BETWEEN_PROFILES apart, run-times
    overlapped across a small pool.

    CRASH-SAFETY: if ``ck_path`` is given, each profile's raw posts are appended
    to a local JSONL checkpoint keyed by profile URL the instant they come back,
    and already-fetched URLs are skipped on a re-run — so a mid-pull crash or
    credit-exhaustion never re-pays for a profile already scraped.
    """
    done: Dict[str, object] = checkpoint_load(ck_path) if ck_path else {}
    posts: List[dict] = []
    pending: List[str] = []
    for url in profile_urls:
        if url in done and isinstance(done[url], list):
            posts.extend(_normalize_profile_post(p, source) for p in done[url])
        else:
            pending.append(url)
    if done and pending:
        print(f"  Resuming from checkpoint: {len(profile_urls) - len(pending)} {source} profile(s) already pulled.")

    def scrape_one(url: str) -> dict:
        return _post_scraper.scrape_linkedin_profile_posts(
            profile_url=url,
            max_posts=max_per_profile,
            days_back=days_back,
        )

    def _persist(_idx, url, res, err):
        if err:
            print(f"  {source} profile {url} failed: {err}")
            return
        raw = (res or {}).get("posts", [])
        if ck_path:
            checkpoint_append(ck_path, url, raw)
        posts.extend(_normalize_profile_post(p, source) for p in raw)

    _map_rate_limited(
        scrape_one, pending,
        min_interval=SLEEP_BETWEEN_PROFILES, max_workers=ICP_CONCURRENCY,
        on_result=_persist,
    )
    return posts


def _search_keywords(keywords: List[str], sort: str, max_posts: int,
                     ck_path: Optional[str] = None) -> List[dict]:
    """Run posts-search for each keyword concurrently (starts spaced), in order.

    CRASH-SAFETY: if ``ck_path`` is given, each keyword's raw result is
    checkpointed keyed by keyword the instant it comes back, and already-searched
    keywords are skipped on a re-run — so a mid-search crash never re-pays for a
    keyword already fetched. The checkpoint key embeds the sort so a prospect and
    engagement run against the same keyword never collide.
    """
    done: Dict[str, object] = checkpoint_load(ck_path) if ck_path else {}
    results: List[dict] = []
    pending: List[str] = []
    for kw in keywords:
        key = f"{sort}::{kw}"
        if key in done and isinstance(done[key], dict):
            results.append(done[key])
        else:
            pending.append(kw)
    if done and pending:
        print(f"  Resuming from checkpoint: {len(keywords) - len(pending)} keyword(s) already searched.")

    def search_one(kw: str) -> dict:
        return _search_scraper.search_linkedin_posts(kw, sort=sort, max_posts=max_posts)

    def _persist(_idx, kw, res, err):
        if err:
            print(f"  Search keyword '{kw}' failed: {err}")
            return
        if res:
            if ck_path:
                checkpoint_append(ck_path, f"{sort}::{kw}", res)
            results.append(res)

    _map_rate_limited(
        search_one, pending,
        min_interval=SEARCH_MIN_INTERVAL, max_workers=SEARCH_CONCURRENCY,
        on_result=_persist,
    )
    return results


def _hours_since(timestamp_ms: Optional[float]) -> Optional[float]:
    if not timestamp_ms:
        return None
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    return max((now_ms - timestamp_ms) / 3_600_000, 0.0)


def _engagement_velocity(post: dict) -> float:
    """Engagement per hour since posting — surfaces *rising* posts over already-
    saturated ones. Age is floored (VELOCITY_MIN_HOURS) so a post from minutes ago
    with a handful of reactions can't out-rank a genuinely hot one."""
    hours = _hours_since(post.get("timestamp_ms"))
    if hours is None:
        return 0.0
    return (post["reactions"] + post["comments"]) / max(hours, VELOCITY_MIN_HOURS)


def _drop_saturated(posts: List[dict], max_comments: int) -> List[dict]:
    """Posts past a comment threshold are 'buried' — a new comment won't be seen,
    so they're poor targets no matter how high their engagement. <= 0 disables
    (guards against a negative value silently dropping every post)."""
    if max_comments <= 0:
        return posts
    return [p for p in posts if p["comments"] <= max_comments]


def _rank_by_velocity(posts: List[dict]) -> List[dict]:
    """Stamp each post with its engagement velocity and sort rising-first.

    Shared by the two reach-play sources (trending + authority) so the ranking
    rule lives in one place."""
    for p in posts:
        p["velocity"] = round(_engagement_velocity(p), 2)
    posts.sort(key=lambda p: p["velocity"], reverse=True)
    return posts


def pull_trending_posts(keywords: List[str], max_per_keyword: int, days_back: int,
                        commentable_max_comments: int = 0,
                        ck_path: Optional[str] = None) -> List[dict]:
    """Search genre keywords, keep recent + still-commentable, rank by engagement velocity.

    Ranking by velocity (engagement / hours-since-posted) rather than absolute
    engagement favors *rising* posts where an early comment is still visible,
    instead of saturated posts where it lands at comment #300.
    """
    results = _search_keywords(keywords, sort="relevance", max_posts=max_per_keyword,
                               ck_path=ck_path)
    posts: List[dict] = []
    for r in results:
        for p in r.get("posts", []):
            posts.append(_normalize_search_post(p, "trending"))

    cutoff_ms = (datetime.now(timezone.utc) - timedelta(days=days_back)).timestamp() * 1000
    posts = [p for p in posts if p.get("timestamp_ms") and p["timestamp_ms"] >= cutoff_ms]
    posts = _drop_saturated(posts, commentable_max_comments)
    return _rank_by_velocity(posts)


def pull_signal_posts(keywords: List[str], max_per_keyword: int, days_back: int,
                      commentable_max_comments: int = 0,
                      ck_path: Optional[str] = None) -> List[dict]:
    """Search signal keywords (date sort — intent recency matters), keep recent + commentable."""
    results = _search_keywords(keywords, sort="date_posted", max_posts=max_per_keyword,
                               ck_path=ck_path)
    posts: List[dict] = []
    for r in results:
        for p in r.get("posts", []):
            posts.append(_normalize_search_post(p, "signal"))

    cutoff_ms = (datetime.now(timezone.utc) - timedelta(days=days_back)).timestamp() * 1000
    posts = [p for p in posts if p.get("timestamp_ms") and p["timestamp_ms"] >= cutoff_ms]
    posts = _drop_saturated(posts, commentable_max_comments)
    return posts


def pull_authority_posts(profile_urls: List[str], max_per_profile: int, days_back: int,
                         commentable_max_comments: int = 0,
                         ck_path: Optional[str] = None) -> List[dict]:
    """Pull posts from the curated authority accounts, drop buried threads, rank by velocity.

    Reach play: you want to comment *early* on a big account's rising post, so we
    rank their recent posts by engagement velocity just like the trending source.
    """
    posts = pull_profile_posts(profile_urls, max_per_profile, days_back, "authority",
                               ck_path=ck_path)
    posts = _drop_saturated(posts, commentable_max_comments)
    return _rank_by_velocity(posts)


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

# Per-intent guidance injected into the scoring prompt. The two plays want
# fundamentally different comments, so the "angle" instruction differs.
_INTENT_ANGLE_GUIDANCE = {
    "prospect": (
        "angle: ONE short sentence — how do I comment so THIS PERSON (a prospect I want a "
        "reply from) notices and remembers me? Lead with a genuine, personal reaction that "
        "shows I read their post and get their world; surface a hook from my past work only "
        'if it fits naturally. Warm and human, NOT audience-grabbing or salesy. If nothing '
        'specific applies, write "warm presence — <the genuine reaction I\'d leave>".'
    ),
    "engagement": (
        "angle: ONE short sentence — what value-add or sharp take would make MY comment stand "
        "out to THIS AUTHOR'S AUDIENCE and earn me reach/visibility? Aim to add an insight, a "
        "concrete example, or a crisp question that their followers would upvote — not generic "
        'praise. If a hook from my past work strengthens it, use it; else write '
        '"value-add — <the insight/question I\'d contribute>".'
    ),
}


SCORE_CHUNK_SIZE = 40  # posts per Claude call — keeps each reply well under max_tokens
# The full ICP context is re-sent in EVERY scoring chunk. Cap it so a large
# context.md doesn't multiply input-token spend across dozens of chunks; the
# leading section carries the genre/audience signal the scorer actually needs.
MAX_CONTEXT_CHARS = 4000


def score_relevance(posts: List[dict], project_context: str, client: anthropic.Anthropic,
                    intent: str = "prospect", ck_path: Optional[str] = None) -> List[Optional[dict]]:
    """Score posts in chunks so one long run can't truncate the reply mid-array.

    INPUT TRIMMING: the project context is capped to MAX_CONTEXT_CHARS before it's
    injected into every chunk (see _score_chunk), so a large context.md doesn't
    inflate spend across many chunks.

    CRASH-SAFETY: if ``ck_path`` is given, each post's score is checkpointed keyed
    by post_id the instant its chunk returns, and already-scored posts are skipped
    on a re-run — so a mid-scoring crash never re-pays Claude for a post already
    scored.
    """
    if len(project_context) > MAX_CONTEXT_CHARS:
        project_context = project_context[:MAX_CONTEXT_CHARS]

    done: Dict[str, object] = checkpoint_load(ck_path) if ck_path else {}

    def _pid(p: dict) -> str:
        return p.get("post_id") or p.get("post_url") or ""

    scores: List[Optional[dict]] = [None] * len(posts)
    pending: List[int] = []
    for i, p in enumerate(posts):
        pid = _pid(p)
        if pid and pid in done:
            scores[i] = done[pid]  # may be None (a prior chunk that failed to parse)
        else:
            pending.append(i)
    if done and len(pending) < len(posts):
        print(f"  Resuming from checkpoint: {len(posts) - len(pending)} post(s) already scored.")

    for start in range(0, len(pending), SCORE_CHUNK_SIZE):
        idx_chunk = pending[start:start + SCORE_CHUNK_SIZE]
        chunk = [posts[i] for i in idx_chunk]
        if start:
            print(f"  ...scoring posts {start + 1}-{start + len(chunk)} of {len(pending)}")
        chunk_scores = _score_chunk(chunk, project_context, client, intent)
        for i, sc in zip(idx_chunk, chunk_scores):
            scores[i] = sc
            pid = _pid(posts[i])
            if ck_path and pid:
                checkpoint_append(ck_path, pid, sc)
    return scores


def _score_chunk(posts: List[dict], project_context: str, client: anthropic.Anthropic,
                 intent: str = "prospect") -> List[Optional[dict]]:
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

    if intent == "engagement":
        goal_line = ("My goal this run is REACH: I want to comment on posts so that the "
                     "author's audience sees me and my own account gains engagement.")
    else:
        goal_line = ("My goal this run is to WARM UP PROSPECTS I'm already reaching out to: "
                     "commenting on their posts so they recognize me and are more likely to reply.")
    angle_guidance = _INTENT_ANGLE_GUIDANCE.get(intent, _INTENT_ANGLE_GUIDANCE["prospect"])

    prompt = f"""You are helping me decide which LinkedIn posts to comment on to build credibility in my space. Use my project context below to infer my genre, audience, and what kinds of posts are relevant — do NOT assume any specific industry.

{goal_line}

My project context (everything I have worked on, and what my project is about):
{project_context}

For each post below, answer:
- worth_commenting: true/false. True if the post is substantive and a thoughtful comment would add value. False for vanity, generic platitudes, or pure self-promotion.
- author_relevant: true/false. True if the AUTHOR is someone whose audience/network is worth being visible in — a potential buyer, a peer, or an influencer in my space (judge from their headline and what they post about). False for people outside my space who merely went viral (generic motivation, unrelated news, etc.). If Source is "icp" or "authority", the author is already a hand-picked target — set true unless the post is clearly off-topic personal content.
- summary: ONE short sentence describing what the post itself is actually about (the gist), so I know what I'd be commenting on without opening it.
- relevance: ONE short sentence — why is this post relevant to my work or audience?
- {angle_guidance}

Posts:
{posts_block}

Return a JSON array — one object per post — with keys: index (1-based), worth_commenting, author_relevant, summary, relevance, angle.
Return only valid JSON, no explanation."""

    resp = client.messages.create(
        model=CLAUDE_MODEL, max_tokens=8000,
        temperature=0,  # classification — keep verdicts stable run-to-run
        messages=[{"role": "user", "content": prompt}],
    )
    try:
        parsed = json.loads(_strip_json_fence(resp.content[0].text))
    except json.JSONDecodeError:
        # One malformed reply shouldn't discard every scraped post; score nothing
        # for this chunk and let the caller report fewer matches.
        print("  ! Couldn't parse the relevance scores from Claude — skipping this batch.")
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
    "Run Date", "Post URL", "Author", "Source", "Intent", "Posted",
    "Why relevant", "My angle", "Reactions", "Comments", "Status", "About the post",
]


def _author_ok(score: dict) -> bool:
    """Whether a post's author passes the relevance gate.

    Fail-open: a missing OR explicitly-null `author_relevant` counts as relevant
    (a model omission must not silently drop every post). Only an explicit false
    value filters the post out.
    """
    val = score.get("author_relevant", True)
    return True if val is None else bool(val)


def read_profile_urls(store: TabularStore, url_column: str) -> List[str]:
    rows = store.read_all()
    if not rows:
        return []
    headers = rows[0]
    idx = find_col(headers, url_column)
    if idx is None:
        print(f"  Column '{url_column}' not found in headers: {headers}")
        return []
    return [cell(r, idx) for r in rows[1:] if cell(r, idx).startswith("http")]


def append_to_sheet(store: TabularStore, posts: List[dict],
                    scores: List[Optional[dict]], intent_label: str) -> int:
    existing = store.read_all() or []

    existing_urls: set = set()
    if existing:
        url_idx = find_col(existing[0], "Post URL")
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
        # Gate on both axes: substantive post AND an author worth being seen by.
        # _author_ok is fail-open so a model omission/null can't nuke the run —
        # worth_commenting still filters.
        if not s or not s.get("worth_commenting") or not _author_ok(s):
            continue
        new_rows.append([
            run_date,
            p["post_url"],
            p["author_name"],
            p["source"],
            intent_label,
            p["posted_at"],
            s.get("relevance") or "",
            s.get("angle") or "",
            str(p["reactions"]),
            str(p["comments"]),
            "",
            s.get("summary") or "",
        ])

    if not new_rows:
        return 0

    # Align by header name (writes SHEET_HEADERS if the store is empty; warns
    # and drops values whose column doesn't exist in a foreign-schema store).
    store.append_mapped(SHEET_HEADERS, [dict(zip(SHEET_HEADERS, r)) for r in new_rows])
    return len(new_rows)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _pause(message: str) -> None:
    input(f"\n[INTERACTIVE] {message} — press Enter to continue, Ctrl+C to abort.\n")


# The workflow runs ONE intent per run. Each maps to a curated profile list + a
# discovery keyword source, and to the label written in the sheet's Intent column.
INTENT_LABELS = {
    "prospect": "prospect — warm for reply",
    "engagement": "reach — borrow audience",
}


def _prompt_intent() -> str:
    print("\nWhat's the intent for this run?")
    print("  1) prospect   — warm up ICPs/prospects you're reaching out to (comment on THEIR posts so they reply)")
    print("  2) engagement — get reach on your own account (comment on big accounts + trending topics)")
    while True:
        choice = input("> ").strip().lower()
        if choice in ("1", "prospect", "p"):
            return "prospect"
        if choice in ("2", "engagement", "e"):
            return "engagement"
        print("Please enter 1 (prospect) or 2 (engagement).")


def _load_keywords(workflow_dir: str, filename: str, ctx_section: Optional[str] = None,
                   captured_body: str = "") -> List[str]:
    """
    Resolution order:
      1. context.md `## {ctx_section}` (one keyword per line)  ← new, preferred
      2. answers typed at the setup prompt this run but not saved to context.md
      3. /Context/<project>/<filename>.json (legacy per-project override)
      4. workflow_dir/<filename>.json (default, kept for backward compat)
    """
    if ctx_section:
        body = _section_body(_read_context_file(), ctx_section) or captured_body
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
    parser.add_argument("--intent", choices=["prospect", "engagement"],
                        help="prospect = warm your ICPs/prospects for a reply; "
                             "engagement = comment on big accounts + trends for reach. "
                             "Required in auto mode; prompted in interactive mode.")
    parser.add_argument("--icp-sheet-id")
    parser.add_argument("--icp-sheet-name")
    parser.add_argument("--icp-url-column")
    parser.add_argument("--authority-sheet-id")
    parser.add_argument("--authority-sheet-name")
    parser.add_argument("--authority-url-column")
    parser.add_argument("--output-sheet-id")
    parser.add_argument("--output-sheet-name")
    parser.add_argument("--output-csv", default=None,
                        help="Write results to a local CSV instead of a Google Sheet")
    parser.add_argument("--curated-csv", default=None,
                        help="Read curated profile URLs from a local CSV instead of a sheet")
    parser.add_argument("--curated-url-column", default=None,
                        help="Column holding profile URLs in --curated-csv (default 'LinkedIn URL')")
    parser.add_argument("--days-back", type=int)
    parser.add_argument("--max-per-profile", type=int)
    parser.add_argument("--max-per-keyword", type=int)
    parser.add_argument("--commentable-max-comments", type=int,
                        help="Drop posts with more comments than this (buried threads). 0 disables.")
    parser.add_argument("--skip-icp", action="store_true", help="Prospect intent: skip the ICP profile source.")
    parser.add_argument("--skip-authority", action="store_true", help="Engagement intent: skip the authority-accounts source.")
    parser.add_argument("--skip-trending", action="store_true", help="Engagement intent: skip the genre-keyword search.")
    parser.add_argument("--skip-signal", action="store_true", help="Prospect intent: skip the signal-keyword search.")
    parser.add_argument("--auto", action="store_true",
                        help="Run non-interactively. Errors out if context.md is missing required sections.")
    parser.add_argument("--max-spend", type=float, default=None,
                        help="Auto-mode only: abort before scraping if the estimated worst-case "
                             "Apify cost exceeds this many USD. No effect in interactive mode "
                             "(where you confirm the estimate directly).")
    args = parser.parse_args()

    if args.output_csv and args.output_sheet_id:
        print("ERROR: pass either --output-csv or --output-sheet-id, not both.")
        sys.exit(1)

    interactive = args.mode == "interactive"

    # ---- Intent — one play per run. Prompted in interactive mode, required in auto.
    # Resolved up front so context backfill only asks for the sections this intent uses.
    intent = args.intent
    if not intent:
        if interactive:
            intent = _prompt_intent()
        else:
            print("\nERROR: no --intent given. This workflow runs one intent per run:\n"
                  "  --intent prospect    (warm your ICPs/prospects for a reply)\n"
                  "  --intent engagement  (comment on big accounts + trends for reach)\n"
                  "Pass one, or use --mode interactive to be asked.")
            sys.exit(2)
    intent_label = INTENT_LABELS[intent]

    # Pre-step: backfill the context sections THIS intent needs before doing any
    # work. Only interactive mode may prompt — a default (auto-mode) run must
    # never block on stdin, so it errors out like --auto if sections are missing.
    captured = ensure_context_complete(auto=args.auto or not interactive, intent=intent)

    workflow_dir = os.path.dirname(os.path.abspath(__file__))

    with open(os.path.join(workflow_dir, "config.json")) as f:
        cfg = json.load(f)

    out_sheet_id    = args.output_sheet_id or cfg["output_sheet"]["id"]
    out_sheet_name  = args.output_sheet_name or cfg["output_sheet"]["tab"]
    days_back       = args.days_back       or cfg["days_back"]
    max_per_profile = args.max_per_profile or cfg["max_per_profile"]
    max_per_keyword = args.max_per_keyword or cfg["max_per_keyword"]
    trending_top_n  = cfg.get("trending_top_n", 30)
    authority_top_n = cfg.get("authority_top_n", trending_top_n)
    # 0 is a meaningful value (disable the cap), so honor an explicit flag over the default.
    commentable_max_comments = (args.commentable_max_comments
                                if args.commentable_max_comments is not None
                                else cfg.get("commentable_max_comments", 0))

    print(f"Mode: {args.mode} | Intent: {intent} | Days back: {days_back}")

    # ---- Resolve this intent's curated profile list + discovery keyword source.
    # Only the chosen intent's keyword list is loaded (the other is never used).
    #   prospect   → ICP list (warm)           + signal phrases (find new prospects)
    #   engagement → authority accounts (reach) + genre keywords (find big posts)
    if intent == "prospect":
        cur_id  = args.icp_sheet_id   or cfg["icp_sheet"]["id"]
        cur_tab = args.icp_sheet_name or cfg["icp_sheet"]["tab"]
        cur_col = args.icp_url_column or cfg["icp_sheet"]["url_column"]
        cur_label = "ICP/prospect"
        skip_curated   = args.skip_icp
        disc_keywords  = _load_keywords(workflow_dir, "signal_keywords.json",
                                        ctx_section="LinkedIn Comment Signal Keywords",
                                        captured_body=captured.get("signal_keywords", ""))
        skip_discovery = args.skip_signal
        disc_label     = "SIGNAL"
    else:
        acfg = cfg.get("authority_sheet", {})
        cur_id  = args.authority_sheet_id   or acfg.get("id", "TODO_PASTE_AUTHORITY_SHEET_ID")
        cur_tab = args.authority_sheet_name or acfg.get("tab", "Sheet1")
        cur_col = args.authority_url_column or acfg.get("url_column", "LinkedIn URL")
        cur_label = "authority"
        skip_curated   = args.skip_authority
        disc_keywords  = _load_keywords(workflow_dir, "genre_keywords.json",
                                        ctx_section="LinkedIn Comment Genre Keywords",
                                        captured_body=captured.get("genre_keywords", ""))
        skip_discovery = args.skip_trending
        disc_label     = "TRENDING"

    all_posts: List[dict] = []

    # Curated profile URLs are read up front so the spend estimate can size the
    # (expensive) profile-posts fan-out before anything is billed.
    curated_urls: List[str] = []
    if not skip_curated:
        if args.curated_csv:
            cur_store = TabularStore(csv_path=args.curated_csv)
            cur_col = args.curated_url_column or cur_col or "LinkedIn URL"
        elif "TODO" not in cur_id:
            cur_store = TabularStore(sheet_id=cur_id, sheet_name=cur_tab)
        else:
            cur_store = None
        if cur_store is None:
            print(f"[{cur_label}] No profile source configured — skipping the curated-profile source.")
        else:
            print(f"[{cur_label}] Reading profile URLs from {cur_store.label()}...")
            raw_urls = read_profile_urls(cur_store, cur_col)
            # Dedupe — duplicate URLs would otherwise pay for the same run twice.
            curated_urls = list(dict.fromkeys(raw_urls))
            dropped = len(raw_urls) - len(curated_urls)
            note = f" ({dropped} duplicate(s) skipped)" if dropped else ""
            print(f"  Found {len(curated_urls)} {cur_label} profile URLs{note}.")

    # ---- Spend preview (only this intent's two sources). Worst case = every
    # requested item returned + billed.
    disc_items = 0 if skip_discovery else len(disc_keywords) * max_per_keyword
    spend_items = [
        (f"{cur_label} profile posts", "linkedin_profile_posts", len(curated_urls) * max_per_profile),
        (f"{disc_label.title()} post search", "linkedin_post_search", disc_items),
    ]
    # Auto mode has no y/N gate, so enforce a spend ceiling here: if the worst-case
    # estimate exceeds --max-spend, abort before a single actor runs. Interactive
    # mode is unchanged — the user confirms the printed estimate directly.
    if not interactive and args.max_spend is not None:
        _, est_total = estimate_apify_cost(spend_items)
        if est_total > args.max_spend:
            print(f"\nERROR: estimated worst-case Apify cost ${est_total:.2f} exceeds "
                  f"--max-spend ${args.max_spend:.2f}. Aborted before any Apify runs.")
            print("Raise --max-spend, or narrow the run (fewer profiles/keywords, "
                  "lower --max-per-profile / --max-per-keyword) and re-run.")
            sys.exit(3)
    if not preview_and_confirm(spend_items, interactive=interactive):
        print("Aborted — no Apify runs were started.")
        return

    # ---- Sources for this intent. The curated list uses the profile-posts
    # actor; discovery uses the posts-search actor. In auto mode both run
    # concurrently (different actors → no shared throttle); interactive runs
    # them sequentially so the between-source pauses make sense.

    # Checkpoint files are keyed by the output target + intent so concurrent /
    # sequential runs against different sheets don't share a checkpoint. Each paid
    # actor pull and each Claude score is saved the instant it returns; a crash /
    # credit-exhaustion mid-run leaves everything done-so-far on disk, and a
    # re-run skips it — never re-paying for work already completed.
    run_key = f"{args.output_csv or out_sheet_id}_{intent}"
    curated_ck   = checkpoint_path(f"linkedin_comment_helper_curated_{run_key}")
    discovery_ck = checkpoint_path(f"linkedin_comment_helper_discovery_{run_key}")
    score_ck     = checkpoint_path(f"linkedin_comment_helper_score_{run_key}")

    def run_curated() -> List[dict]:
        if not curated_urls:
            return []
        print(f"[{cur_label}] Pulling posts (≥{SLEEP_BETWEEN_PROFILES}s between starts, up to {ICP_CONCURRENCY} in flight)...")
        if intent == "engagement":
            # Reach play — rank the big accounts' posts by velocity, comment early.
            posts = pull_authority_posts(curated_urls, max_per_profile, days_back,
                                         commentable_max_comments, ck_path=curated_ck)[:authority_top_n]
            print(f"  Got {len(posts)} authority posts (top {authority_top_n} by engagement velocity).")
        else:
            # Warm play — presence matters, so keep all recent posts (no velocity cull).
            posts = pull_profile_posts(curated_urls, max_per_profile, days_back, "icp",
                                       ck_path=curated_ck)
            print(f"  Got {len(posts)} prospect posts.")
        return posts

    def run_discovery() -> List[dict]:
        if skip_discovery:
            return []
        print(f"[{disc_label}] Searching {len(disc_keywords)} {disc_label.lower()} keywords...")
        if intent == "engagement":
            posts = pull_trending_posts(disc_keywords, max_per_keyword, days_back,
                                        commentable_max_comments, ck_path=discovery_ck)[:trending_top_n]
            print(f"  Got {len(posts)} trending posts (top {trending_top_n} by engagement velocity).")
        else:
            posts = pull_signal_posts(disc_keywords, max_per_keyword, days_back,
                                      commentable_max_comments, ck_path=discovery_ck)
            print(f"  Got {len(posts)} signal posts.")
        return posts

    if interactive:
        all_posts.extend(run_curated())
        if not skip_curated:
            _pause(f"{cur_label} pull complete")
        all_posts.extend(run_discovery())
        if not skip_discovery:
            _pause(f"{disc_label.lower()} pull complete")
    else:
        with ThreadPoolExecutor(max_workers=2) as ex:
            curated_fut = ex.submit(run_curated)
            discovery_fut = ex.submit(run_discovery)
            all_posts.extend(curated_fut.result())
            all_posts.extend(discovery_fut.result())

    # ---- Dedupe
    all_posts = dedupe_posts(all_posts)
    print(f"\n[DEDUPE] {len(all_posts)} unique posts across both sources.")

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
    scores = score_relevance(all_posts, project_context, client, intent=intent, ck_path=score_ck)
    surfaced = sum(1 for s in scores
                   if s and s.get("worth_commenting") and _author_ok(s))
    print(f"  {surfaced} of {len(all_posts)} posts worth commenting AND by a relevant author.")

    # Save scores alongside posts
    with open(os.path.join(workflow_dir, "results.json"), "w") as f:
        json.dump({
            "run_date": datetime.now().isoformat(),
            "posts": all_posts,
            "scores": scores,
        }, f, indent=2, ensure_ascii=False)

    if interactive:
        _pause("Scoring complete — review results.json before sheet write")

    # ---- Append to sheet (or CSV)
    if args.output_csv:
        out_store = TabularStore(csv_path=args.output_csv)
    elif "TODO" not in out_sheet_id:
        out_store = TabularStore(sheet_id=out_sheet_id, sheet_name=out_sheet_name)
    else:
        print("\n[OUTPUT] No output sheet or CSV configured. Saved to results.json only.")
        return

    print(f"\n[OUTPUT] Appending to {out_store.label()}...")
    n = append_to_sheet(out_store, all_posts, scores, intent_label)
    print(f"  Appended {n} new rows.")


if __name__ == "__main__":
    main()
