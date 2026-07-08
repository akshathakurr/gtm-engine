"""
Content Idea Finder Workflow — daily content ideas for LinkedIn + Twitter.

Project-agnostic. Two modes:

  1. daily   — pulls trends from Twitter + Hacker News, discovers 5 topics,
               then checks what trusted creators are saying about each topic.
  2. idea    — takes a user-supplied seed idea, researches what others are saying,
               checks trusted creator views on that topic, returns 1 idea card.

Flow (daily):
  Phase 1 — Fetch Twitter trends + HN → Claude discovers N topics (with keywords)
  Phase 2 — Fetch creator tweets → keyword-match against each topic
  Phase 3 — Claude classifies each enriched topic into a full idea card

Each idea card contains: topic, genre, content_type, platform, why_now,
source_quotes (trend/HN refs), creator_views (what trusted creators said),
suggested_angle. Hook + body are left empty for the writing skill.

Genres:        Trendy / Trust building / Engineering heavy / Project Showcase / Engagement
Content types: Long form / Short-mid / Article / Blog
Platforms:     Twitter / LinkedIn / Both

Usage:
  python3 workflow.py --mode daily --sheet-id SHEET_ID
  python3 workflow.py --mode daily --sheet-id SHEET_ID --num-ideas 5 --lookback-days 3
  python3 workflow.py --mode idea  --idea "the case for small models" --sheet-id SHEET_ID
  python3 workflow.py --mode daily --sheet-id SHEET_ID --skip-creators
  python3 workflow.py --mode daily --sheet-id SHEET_ID --skip-trends
  python3 workflow.py --mode daily --sheet-id SHEET_ID --skip-hn
"""

import os
import sys
import json
import time
import argparse
from datetime import datetime, timezone
from typing import List, Dict

import anthropic

from config import CLAUDE_MODEL, CONTEXT_DIR
from workflows._common import (
    strip_json_fence as _strip_json_fence, CONTEXT_FILE,
    read_context_file as _read_context_file,
    append_to_context_file as _append_to_context_file,
    section_body as _section_body,
    preview_and_confirm, TabularStore,
)
from scrapers.twitter_profile_scraper.scraper import scrape_twitter_profiles_batch
from scrapers.twitter_research_scraper.scraper import search_tweets
from scrapers.hacker_news_scraper.scraper import scrape_hn

_HERE    = os.path.dirname(__file__)
_OUTPUTS = os.path.join(_HERE, "outputs")

SHEET_HEADERS = [
    "Date", "Idea ID", "Topic", "Genre", "Content Type", "Platform",
    "Why Now", "Suggested Angle", "Source URLs", "Hook", "Body",
]


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_json(filename: str) -> Dict:
    with open(os.path.join(_HERE, filename)) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Interactive pre-step — for each input, check context.md → prompt → fall back
# to built-in JSON defaults (and show the defaults so the user knows what's
# being used).
# ---------------------------------------------------------------------------

def _read_multiline_input(prompt_text: str) -> str:
    print(prompt_text)
    print("(End with an empty line. Press Enter on an empty line to skip and use the built-in defaults.)")
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


def _parse_lines(body: str) -> List[str]:
    """Each non-empty, non-comment line becomes an entry."""
    out: List[str] = []
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("(") or line.startswith("-"):
            continue
        out.append(line)
    return out


def _parse_creator_lines(body: str) -> List[Dict[str, str]]:
    """Each line is 'Name | handle' or just 'handle'."""
    out: List[Dict[str, str]] = []
    for line in _parse_lines(body):
        if "|" in line:
            name, handle = [x.strip() for x in line.split("|", 1)]
        else:
            name, handle = line, line
        handle = handle.lstrip("@").strip()
        if handle:
            out.append({"name": name, "twitter": handle, "notes": ""})
    return out


def _parse_query_lines(body: str) -> List[Dict[str, str]]:
    """Each line is 'bucket | query'."""
    out: List[Dict[str, str]] = []
    for line in _parse_lines(body):
        if "|" not in line:
            continue
        bucket, query = [x.strip() for x in line.split("|", 1)]
        if bucket and query:
            out.append({"bucket": bucket, "query": query})
    return out


def _parse_hn_query_lines(body: str) -> List[Dict[str, str]]:
    """Each line is 'bucket | query | story_type | sort_by'. Last two optional."""
    out: List[Dict[str, str]] = []
    for line in _parse_lines(body):
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2:
            continue
        bucket, query = parts[0], parts[1]
        story_type = parts[2] if len(parts) > 2 and parts[2] else "story"
        sort_by    = parts[3] if len(parts) > 3 and parts[3] else "relevance"
        if bucket:
            out.append({"bucket": bucket, "query": query, "story_type": story_type, "sort_by": sort_by})
    return out


def _preview(items: List[str], n: int = 5) -> str:
    head = items[:n]
    suffix = f" (+{len(items) - n} more)" if len(items) > n else ""
    return ", ".join(head) + suffix


def resolve_inputs(auto: bool) -> Dict:
    """
    For each input the workflow needs (genres, creators, trend queries, HN queries):
      1. Look in context.md — use if present.
      2. Else (and not --auto), prompt the user.
         · If they answer, save to context.md and use.
         · If they skip, fall back to built-in JSON defaults and PRINT them so
           the user knows exactly what's being used.

    Under --auto: skip prompts, fall back silently to JSON defaults.
    Returns a dict with: genres (List[str]), creators, trend_queries, hn_queries.
    """
    text = _read_context_file()

    creators_default      = load_json("creators.json").get("creators", [])
    trends_default        = load_json("trend_queries.json").get("queries", [])
    trends_min_likes      = load_json("trend_queries.json").get("min_likes", 100)
    hn_default            = load_json("hn_queries.json").get("queries", [])
    hn_min_points         = load_json("hn_queries.json").get("min_points", 100)

    show_intro = False
    inputs: Dict = {
        "genres": [],
        "creators": creators_default,
        "trend_queries": trends_default,
        "hn_queries": hn_default,
        "min_likes": trends_min_likes,
        "min_points": hn_min_points,
    }

    sections = [
        {
            "key": "genres",
            "header": "Content Genres",
            "prompt": "What genres / topics do you want content ideas for? One per line (e.g. 'Startups', 'AI engineering', 'Climate tech', 'Design'). These drive what trends Claude considers on-topic.",
            "parser": _parse_lines,
            "default": ["Startups", "Engineering", "AI", "Building"],
            "default_label": "Startups, Engineering, AI, Building",
        },
        {
            "key": "creators",
            "header": "Content Trusted Creators",
            "prompt": "Whose Twitter voices do you want to mine for context? One per line, format 'Name | handle' (e.g. 'Paul Graham | paulg').",
            "parser": _parse_creator_lines,
            "default": creators_default,
            "default_label": _preview([c["name"] for c in creators_default]),
        },
        {
            "key": "trend_queries",
            "header": "Content Trend Queries",
            "prompt": "Twitter search queries to pull trending posts. One per line, format 'bucket | query' (e.g. 'startups | seed funding announcement').",
            "parser": _parse_query_lines,
            "default": trends_default,
            "default_label": _preview([f"[{q['bucket']}] {q['query']}" for q in trends_default]),
        },
        {
            "key": "hn_queries",
            "header": "Content HN Queries",
            "prompt": "Hacker News topics to scan. One per line, format 'bucket | query | story_type | sort_by'. story_type defaults to 'story' (or 'show_hn'); sort_by defaults to 'relevance' (or 'date'). Example: 'ai_building | AI OR LLM OR agent | story | relevance'.",
            "parser": _parse_hn_query_lines,
            "default": hn_default,
            "default_label": _preview([f"[{q['bucket']}] {q.get('query') or q.get('story_type','')}" for q in hn_default]),
        },
    ]

    answers_to_save: Dict[str, str] = {}

    for spec in sections:
        body = _section_body(text, spec["header"])
        if body:
            parsed = spec["parser"](body)
            if parsed:
                inputs[spec["key"]] = parsed
                continue
            # Section exists but couldn't parse — fall through to default

        if auto:
            # Silent fallback under --auto
            inputs[spec["key"]] = spec["default"]
            print(f"  · {spec['header']}: using built-in defaults — {spec['default_label']}")
            continue

        # Interactive: prompt
        if not show_intro:
            print("\n" + "=" * 70)
            print(" Setup — context.md is missing some sections this workflow uses")
            print("=" * 70)
            print("Answer each prompt to personalise the workflow. Press Enter on an")
            print("empty line to skip that section and use the built-in defaults.\n")
            show_intro = True

        print(f"[{spec['header']}]")
        print(spec["prompt"])
        ans = _read_multiline_input("> ")
        print()

        if ans.strip():
            parsed = spec["parser"](ans)
            if parsed:
                inputs[spec["key"]] = parsed
                answers_to_save[spec["header"]] = ans
                continue

        # Skipped or unparseable — show the defaults so the user knows
        inputs[spec["key"]] = spec["default"]
        print(f"  Using built-in defaults for [{spec['header']}]:")
        print(f"    {spec['default_label']}\n")

    if answers_to_save:
        choice = input("Save your answers to context/context.md so you aren't re-asked? [Y/n] ").strip().lower()
        if choice in ("", "y", "yes"):
            for header, body in answers_to_save.items():
                _append_to_context_file(header, body)
            print(f"Saved to {os.path.join(CONTEXT_DIR, CONTEXT_FILE)}.\n")
        else:
            print("Not saved — your answers will be used for this run only.\n")

    return inputs


# ---------------------------------------------------------------------------
# Signal fetchers
# ---------------------------------------------------------------------------

def fetch_trend_tweets(queries: List[Dict], min_likes: int, lookback_days: int, max_per_query: int = 25) -> List[Dict]:
    posts: List[Dict] = []

    print(f"  Searching {len(queries)} Twitter trend queries (min {min_likes} likes)...")
    for i, q in enumerate(queries):
        if i > 0:
            time.sleep(2)  # small gap; actor rotates guest tokens internally

        result = search_tweets(
            query=q["query"],
            max_tweets=max_per_query,
            days_back=lookback_days,
            include_replies=False,
        )
        tweets = result.get("tweets", [])
        kept = [t for t in tweets if (t.get("likes", 0) or 0) >= min_likes]
        print(f"    [{q['bucket']}] '{q['query']}': {len(tweets)} fetched → {len(kept)} above threshold")

        for t in kept:
            author = t.get("author") or {}
            posts.append({
                "source":     "trend",
                "query":      q["query"],
                "bucket":     q["bucket"],
                "author":     author.get("name", ""),
                "handle":     author.get("screen_name", ""),
                "text":       t.get("text", ""),
                "url":        t.get("url", ""),
                "created_at": t.get("created_at", ""),
                "engagement": (t.get("likes", 0) or 0) + (t.get("retweets", 0) or 0) * 2,
            })

    return posts


def fetch_hn_stories(queries: List[Dict], min_points: int, lookback_days: int, max_per_query: int = 30) -> List[Dict]:
    stories: List[Dict] = []

    print(f"  Fetching HN stories for {len(queries)} buckets (min {min_points} points, {lookback_days}d)...")
    for q in queries:
        result = scrape_hn(
            query=q["query"],
            story_type=q.get("story_type", "story"),
            sort_by=q.get("sort_by", "relevance"),
            days_back=lookback_days,
            max_results=max_per_query,
        )
        raw = result.get("stories", [])
        kept = [s for s in raw if (s.get("points", 0) or 0) >= min_points]
        print(f"    [{q['bucket']}] '{q['query'] or q.get('story_type', 'story')}': {len(raw)} fetched → {len(kept)} above threshold")

        for s in kept:
            stories.append({
                "source":     "hn",
                "query":      q["query"],
                "bucket":     q["bucket"],
                "title":      s.get("title", ""),
                "text":       s.get("title", ""),
                "url":        s.get("url") or s.get("hn_url", ""),
                "hn_url":     s.get("hn_url", ""),
                "author":     s.get("author", ""),
                "created_at": s.get("created_at", ""),
                "engagement": s.get("points", 0) or 0,
            })

    return stories


def fetch_creator_posts(creators: List[Dict], lookback_days: int, max_per_profile: int = 25) -> List[Dict]:
    """Pull recent tweets from trusted creators. Called after topics are known."""
    profile_urls = [f"https://twitter.com/{c['twitter']}" for c in creators]

    print(f"  Fetching tweets from {len(profile_urls)} creators ({lookback_days}d lookback)...")
    results = scrape_twitter_profiles_batch(
        profile_urls=profile_urls,
        max_tweets=max_per_profile,
        days_back=lookback_days,
        include_retweets=False,
    )

    posts: List[Dict] = []
    for creator, result in zip(creators, results):
        tweets = result.get("tweets", [])
        if not tweets:
            print(f"    {creator['name']}: 0 tweets ({result.get('errors', [])})")
            continue
        print(f"    {creator['name']}: {len(tweets)} tweets")
        for t in tweets:
            posts.append({
                "source":     "creator",
                "author":     creator["name"],
                "handle":     creator["twitter"],
                "text":       t.get("text", ""),
                "url":        t.get("url", ""),
                "created_at": t.get("created_at", ""),
                "engagement": (t.get("likes", 0) or 0) + (t.get("retweets", 0) or 0) * 2,
            })

    return posts


# ---------------------------------------------------------------------------
# Phase 1 — Discover topics from Twitter trends + HN
# ---------------------------------------------------------------------------

def _build_signal_block(trend_posts: List[Dict], hn_stories: List[Dict]) -> str:
    lines: List[str] = []

    if trend_posts:
        lines.append("=== TWITTER TRENDS ===")
        for i, p in enumerate(trend_posts, 1):
            text = (p.get("text") or "").replace("\n", " ")[:240]
            lines.append(f"T{i}. [{p['bucket']}/@{p['handle']}] [{p['engagement']}eng]: {text}")

    if hn_stories:
        lines.append("\n=== HACKER NEWS ===")
        for i, s in enumerate(hn_stories, 1):
            lines.append(f"H{i}. [{s['bucket']}] [{s['engagement']}pts]: {s['title']}")

    return "\n".join(lines)


def discover_topics(
    trend_posts: List[Dict],
    hn_stories: List[Dict],
    num_ideas: int,
    genres: List[str],
    client: anthropic.Anthropic,
) -> List[Dict]:
    """
    Phase 1: Identify topics from Twitter trends + HN only.
    Returns list of {topic, why_now, source_ids, keywords}.
    Creator lookup happens in Phase 2.
    """
    signal_block = _build_signal_block(trend_posts, hn_stories)

    if not signal_block.strip():
        return []

    genres_block = ", ".join(genres) if genres else "(no genres specified — accept all topics)"
    prompt = f"""You are a content strategist for a creator writing on Twitter and LinkedIn.
The creator writes about: {genres_block}.
Anything clearly outside these genres is off-topic — drop it.

Below are trending signals from Twitter and Hacker News:
{signal_block}

Your task: Identify the top {num_ideas} distinct topics worth writing about.
For each topic, extract 3-5 short keywords or phrases that someone would use when discussing it on Twitter.

Return a JSON array of exactly {num_ideas} objects:
[
  {{
    "topic": "<one-sentence topic>",
    "why_now": "<one sentence — why this is worth posting now>",
    "source_ids": ["T3", "H2"],
    "keywords": ["keyword1", "keyword2", "keyword3"]
  }}
]

Return only valid JSON, no explanation."""

    resp = client.messages.create(
        model=CLAUDE_MODEL, max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    return json.loads(_strip_json_fence(resp.content[0].text))


# ---------------------------------------------------------------------------
# Phase 2 — Match creator views to discovered topics
# ---------------------------------------------------------------------------

def match_creator_views(topics: List[Dict], creator_posts: List[Dict]) -> List[Dict]:
    """
    For each topic, find creator posts that mention any of its keywords.
    No LLM call — pure keyword match. Creator views are context, not discovery signal.
    """
    enriched = []
    for topic in topics:
        keywords = [kw.lower() for kw in topic.get("keywords", [])]
        matched = []
        for post in creator_posts:
            text_lower = (post.get("text") or "").lower()
            if keywords and any(kw in text_lower for kw in keywords):
                matched.append(post)

        enriched.append({**topic, "creator_views": matched})

        if matched:
            names = ", ".join(f"@{p['handle']}" for p in matched[:3])
            suffix = f" (+{len(matched)-3} more)" if len(matched) > 3 else ""
            print(f"    '{topic['topic'][:60]}': {len(matched)} creator match(es) — {names}{suffix}")
        else:
            print(f"    '{topic['topic'][:60]}': no creator matches")

    return enriched


# ---------------------------------------------------------------------------
# Phase 3 — Classify topics into full idea cards
# ---------------------------------------------------------------------------

def classify_ideas(topics: List[Dict], genres: List[str], client: anthropic.Anthropic) -> List[Dict]:
    """
    Phase 3: For each topic (now enriched with creator views), assign genre,
    content_type, platform, and suggested_angle. Creator views inform the angle
    — the goal is differentiation, not repetition of what creators said.
    """
    topics_block = ""
    for i, t in enumerate(topics, 1):
        creator_views = t.get("creator_views", [])
        creator_block = ""
        if creator_views:
            lines = []
            for cv in creator_views[:3]:
                text = (cv.get("text") or "").replace("\n", " ")[:200]
                lines.append(f"  - @{cv['handle']} ({cv['author']}): {text}")
            creator_block = "\nTrusted creator views on this topic:\n" + "\n".join(lines)
        topics_block += f"\nTopic {i}: {t['topic']}\nWhy now: {t['why_now']}{creator_block}\n"

    genres_block = ", ".join(genres) if genres else "(no genres specified)"
    prompt = f"""You are a content strategist for a creator writing on Twitter and LinkedIn.
The creator writes about: {genres_block}.

Here are {len(topics)} discovered topics. Where available, trusted creator views are shown.
Use creator views to understand the existing conversation — then find the founder's differentiated angle.
Do not rehash what the creators already said.

{topics_block}

For each topic, classify and suggest a differentiated angle:

Genres:
  "Trendy topic"      — riding a current wave (multiple sources, fresh)
  "Trust building"    — opinionated take proving expertise (GTM, startups, building, best practices, common mistakes)
  "Engineering heavy" — technical deep dive (reverse engineering, system design, agent internals, code)
  "Project Showcase"  — something the founder built, shown to the world
  "Engagement"        — designed to spark replies (hot take, question, controversial)

Format defaults (override only with clear reason):
  Trendy → Short-mid, Twitter or Both
  Trust building → Long form, LinkedIn or Both
  Engineering heavy → Long form or Article
  Project Showcase → Short-mid, Both
  Engagement → Short-mid, Twitter

Return a JSON array of {len(topics)} objects in the same order as the input topics:
[
  {{
    "genre": "<one of the 5 genres>",
    "content_type": "Long form" | "Short-mid" | "Article" | "Blog",
    "platform": "Twitter" | "LinkedIn" | "Both",
    "suggested_angle": "<one sentence — the founder's differentiated take>"
  }}
]

Return only valid JSON, no explanation."""

    resp = client.messages.create(
        model=CLAUDE_MODEL, max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    classifications = json.loads(_strip_json_fence(resp.content[0].text))

    # The model is asked for one classification per topic in order; if it returns
    # a different count, pad/truncate so every topic survives (blank genre) rather
    # than silently dropping the tail that zip() would.
    if len(classifications) != len(topics):
        print(f"  ! classifier returned {len(classifications)} items for "
              f"{len(topics)} topics; padding to align.")
        classifications = (classifications + [{}] * len(topics))[:len(topics)]

    result = []
    for topic, cls in zip(topics, classifications):
        result.append({
            **topic,
            "genre":           cls.get("genre", ""),
            "content_type":    cls.get("content_type", ""),
            "platform":        cls.get("platform", ""),
            "suggested_angle": cls.get("suggested_angle", ""),
        })
    return result


# ---------------------------------------------------------------------------
# Hydrate source quotes — resolve T/H source IDs to full post data
# ---------------------------------------------------------------------------

def hydrate_source_quotes(
    source_ids: List[str],
    trend_posts: List[Dict],
    hn_stories: List[Dict],
) -> List[Dict]:
    """Resolve ['T3', 'H2'] → list of {author, handle, text, url, posted_at}."""
    quotes: List[Dict] = []
    for sid in source_ids:
        if not sid or len(sid) < 2:
            continue
        prefix, num_str = sid[0], sid[1:]
        try:
            idx = int(num_str) - 1
        except ValueError:
            continue

        if prefix == "T" and 0 <= idx < len(trend_posts):
            p = trend_posts[idx]
            quotes.append({
                "author":    p.get("author", ""),
                "handle":    f"@{p.get('handle', '')}",
                "text":      p.get("text", ""),
                "url":       p.get("url", ""),
                "posted_at": p.get("created_at", ""),
            })
        elif prefix == "H" and 0 <= idx < len(hn_stories):
            s = hn_stories[idx]
            quotes.append({
                "author":    s.get("author", ""),
                "handle":    "HN",
                "text":      s.get("title", ""),
                "url":       s.get("hn_url", ""),
                "posted_at": s.get("created_at", ""),
            })
    return quotes


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def make_idea_id(date_str: str, n: int) -> str:
    return f"{date_str}-{n:02d}"


def write_outputs(ideas: List[Dict], store: TabularStore, run_date: str) -> str:
    os.makedirs(_OUTPUTS, exist_ok=True)
    local_path = os.path.join(_OUTPUTS, f"{run_date}.json")

    with open(local_path, "w") as f:
        json.dump({"date": run_date, "ideas": ideas}, f, indent=2, ensure_ascii=False)
    print(f"  Local JSON → {local_path}")

    rows: List[Dict[str, str]] = []
    for idea in ideas:
        all_urls = (
            [q["url"] for q in idea.get("source_quotes", []) if q.get("url")] +
            [cv["url"] for cv in idea.get("creator_views", []) if cv.get("url")]
        )
        rows.append({
            "Date": run_date,
            "Idea ID": idea.get("idea_id", ""),
            "Topic": idea.get("topic", ""),
            "Genre": idea.get("genre", ""),
            "Content Type": idea.get("content_type", ""),
            "Platform": idea.get("platform", ""),
            "Why Now": idea.get("why_now", ""),
            "Suggested Angle": idea.get("suggested_angle", ""),
            "Source URLs": "\n".join(all_urls),
            "Hook": idea.get("hook", ""),
            "Body": idea.get("body", ""),
        })
    # Map by header name (not fixed position) so rows land in the right columns
    # even if the target tab already has a different column order.
    store.append_mapped(SHEET_HEADERS, rows)
    print(f"  {store.label()} ← {len(rows)} rows appended")

    return local_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Content Idea Finder")
    parser.add_argument("--mode", choices=["daily", "idea"], required=True)
    parser.add_argument("--idea", default=None, help="Seed idea (required for --mode idea)")
    parser.add_argument("--sheet-id", default=None, help="Google Sheet ID (or use --output-csv)")
    parser.add_argument("--sheet-name", default="Sheet1")
    parser.add_argument("--output-csv", default=None,
                        help="Write ideas to a local CSV instead of a Google Sheet")
    parser.add_argument("--num-ideas", type=int, default=5)
    parser.add_argument("--lookback-days", type=int, default=3, help="Days back for Twitter trends + creators")
    parser.add_argument("--hn-lookback-days", type=int, default=7)
    parser.add_argument("--skip-creators", action="store_true")
    parser.add_argument("--skip-trends",   action="store_true")
    parser.add_argument("--skip-hn",       action="store_true")
    parser.add_argument("--auto",          action="store_true",
                        help="Run non-interactively. Skips prompts; falls back to built-in defaults for any context.md section that's empty.")
    args = parser.parse_args()

    if args.mode == "idea" and not args.idea:
        print("ERROR: --idea is required when --mode=idea")
        sys.exit(1)

    if bool(args.sheet_id) == bool(args.output_csv):
        print("ERROR: pass exactly one of --sheet-id or --output-csv")
        sys.exit(1)
    store = TabularStore(sheet_id=args.sheet_id, sheet_name=args.sheet_name,
                         csv_path=args.output_csv)

    client   = anthropic.Anthropic()
    run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Pre-step: resolve genres / creators / trend queries / HN queries from
    # context.md (preferred), prompts (fallback), then built-in JSON defaults.
    inputs = resolve_inputs(auto=args.auto)
    user_genres   = inputs["genres"]
    creators_list = inputs["creators"]
    trend_queries = inputs["trend_queries"]
    hn_queries    = inputs["hn_queries"]
    min_likes     = inputs["min_likes"]
    min_points    = inputs["min_points"]

    # ---- Spend preview. Only Twitter trends + creator pulls hit a paid Apify
    # actor (HN uses the free Algolia API). Worst case bills the actor's 20-item
    # floor per run; 25 is the per-query/per-creator fetch default.
    TWEETS_PER_PULL = 25  # matches fetch_trend_tweets / fetch_creator_posts defaults
    if not preview_and_confirm([
        ("Twitter trend search", "twitter",
         (0 if args.skip_trends else len(trend_queries) * TWEETS_PER_PULL)),
        ("Creator tweet pulls",  "twitter",
         (0 if args.skip_creators else len(creators_list) * TWEETS_PER_PULL)),
    ], interactive=not args.auto):
        print("Aborted — no Apify runs were started.")
        return

    trend_posts: List[Dict] = []
    hn_stories:  List[Dict] = []

    # ======================================================================
    # DAILY MODE
    # ======================================================================
    if args.mode == "daily":

        # --- Phase 1a: Twitter trends ---
        if not args.skip_trends:
            print("\n--- Source: Twitter Trend Search ---")
            trend_posts = fetch_trend_tweets(
                queries=trend_queries,
                min_likes=min_likes,
                lookback_days=args.lookback_days,
            )
            print(f"  Total: {len(trend_posts)} tweets")
        else:
            print("\n--- Source: Twitter trends skipped ---")

        # --- Phase 1b: Hacker News ---
        if not args.skip_hn:
            print("\n--- Source: Hacker News ---")
            hn_stories = fetch_hn_stories(
                queries=hn_queries,
                min_points=min_points,
                lookback_days=args.hn_lookback_days,
            )
            print(f"  Total: {len(hn_stories)} stories")
        else:
            print("\n--- Source: Hacker News skipped ---")

        if not (trend_posts or hn_stories):
            print("\nNo signals collected. Exiting.")
            return

        # --- Phase 1: Discover topics ---
        print(f"\n--- Phase 1: Discovering {args.num_ideas} topics from trends + HN ---")
        raw_topics = discover_topics(trend_posts, hn_stories, args.num_ideas, user_genres, client)
        if not raw_topics:
            print("  LLM returned no topics. Exiting.")
            return
        for t in raw_topics:
            print(f"  → {t['topic'][:80]}")

        # --- Phase 2: Creator views (now that topics are known) ---
        creator_posts: List[Dict] = []
        if not args.skip_creators:
            print("\n--- Phase 2: Fetching creator views ---")
            creator_posts = fetch_creator_posts(
                creators=creators_list,
                lookback_days=args.lookback_days,
            )
            print(f"  Total: {len(creator_posts)} creator tweets")
            print("\n  Matching creator views to topics...")
            enriched_topics = match_creator_views(raw_topics, creator_posts)
        else:
            print("\n--- Phase 2: Creator lookup skipped ---")
            enriched_topics = [{**t, "creator_views": []} for t in raw_topics]

        # --- Phase 3: Classify into full idea cards ---
        print("\n--- Phase 3: Classifying idea cards ---")
        classified = classify_ideas(enriched_topics, user_genres, client)

    # ======================================================================
    # IDEA MODE
    # ======================================================================
    else:
        print(f"\n--- Idea mode: '{args.idea}' ---")

        # Search Twitter for the seed idea
        if not args.skip_trends:
            print("\n--- Searching Twitter for seed idea ---")
            result = search_tweets(
                query=args.idea,
                max_tweets=25,
                days_back=args.lookback_days,
                include_replies=False,
            )
            tweets = result.get("tweets", [])
            print(f"  {len(tweets)} tweets found")
            for t in tweets:
                author = t.get("author") or {}
                trend_posts.append({
                    "source":     "trend",
                    "query":      args.idea,
                    "bucket":     "seed_idea",
                    "author":     author.get("name", ""),
                    "handle":     author.get("screen_name", ""),
                    "text":       t.get("text", ""),
                    "url":        t.get("url", ""),
                    "created_at": t.get("created_at", ""),
                    "engagement": (t.get("likes", 0) or 0) + (t.get("retweets", 0) or 0) * 2,
                })

        # Search HN for the seed idea
        if not args.skip_hn:
            print("\n--- Searching HN for seed idea ---")
            hn_result = scrape_hn(
                query=args.idea,
                story_type="story",
                sort_by="relevance",
                days_back=args.hn_lookback_days,
                max_results=20,
            )
            hn_raw = hn_result.get("stories", [])
            print(f"  {len(hn_raw)} HN stories found")
            for s in hn_raw:
                hn_stories.append({
                    "source":     "hn",
                    "query":      args.idea,
                    "bucket":     "seed_idea",
                    "title":      s.get("title", ""),
                    "text":       s.get("title", ""),
                    "url":        s.get("url") or s.get("hn_url", ""),
                    "hn_url":     s.get("hn_url", ""),
                    "author":     s.get("author", ""),
                    "created_at": s.get("created_at", ""),
                    "engagement": s.get("points", 0) or 0,
                })

        # Build seed topic — topic is the idea itself, keywords extracted from it
        seed_keywords = [w for w in args.idea.lower().split() if len(w) > 3]
        trend_posts.sort(key=lambda x: x.get("engagement", 0), reverse=True)
        hn_stories.sort(key=lambda x: x.get("engagement", 0), reverse=True)
        top_source_ids = (
            [f"T{i+1}" for i in range(min(3, len(trend_posts)))] +
            [f"H{i+1}" for i in range(min(2, len(hn_stories)))]
        )
        seed_topic = {
            "topic":      args.idea,
            "why_now":    "User-provided seed idea",
            "source_ids": top_source_ids,
            "keywords":   seed_keywords,
        }

        # Creator views on the seed idea
        creator_posts: List[Dict] = []
        if not args.skip_creators:
            print("\n--- Fetching creator views on seed idea ---")
            creator_posts = fetch_creator_posts(
                creators=creators_list,
                lookback_days=args.lookback_days,
            )
            print(f"  Total: {len(creator_posts)} creator tweets")
            print("\n  Matching creator views to seed topic...")
            enriched_topics = match_creator_views([seed_topic], creator_posts)
        else:
            enriched_topics = [{**seed_topic, "creator_views": []}]

        # Classify
        print("\n--- Classifying idea card ---")
        classified = classify_ideas(enriched_topics, user_genres, client)

    # ======================================================================
    # HYDRATE + FINALIZE
    # ======================================================================
    print("\n--- Hydrating source quotes ---")
    ideas: List[Dict] = []
    for n, idea in enumerate(classified, 1):
        quotes = hydrate_source_quotes(
            source_ids=idea.get("source_ids", []),
            trend_posts=trend_posts,
            hn_stories=hn_stories,
        )
        ideas.append({
            "idea_id":         make_idea_id(run_date, n),
            "topic":           idea.get("topic", ""),
            "genre":           idea.get("genre", ""),
            "content_type":    idea.get("content_type", ""),
            "platform":        idea.get("platform", ""),
            "why_now":         idea.get("why_now", ""),
            "suggested_angle": idea.get("suggested_angle", ""),
            "source_quotes":   quotes,
            "creator_views":   idea.get("creator_views", []),
            "hook":            "",
            "body":            "",
        })
        print(f"  [{ideas[-1]['idea_id']}] {idea.get('genre', '?')} / {idea.get('content_type', '?')} — {idea.get('topic', '')[:80]}")

    # ======================================================================
    # OUTPUT
    # ======================================================================
    print("\n--- Writing outputs ---")
    write_outputs(ideas, store, run_date)

    print("\n======= Done =======")
    print(f"  {len(ideas)} idea card(s) generated.")
    print("  Hook + body left blank — fill in via writing skill when ready.")


if __name__ == "__main__":
    main()
