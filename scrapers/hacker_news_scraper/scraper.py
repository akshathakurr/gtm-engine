"""
Hacker News Scraper — search stories by keyword or fetch by type (top/new/ask/show).

Uses the free Algolia HN Search API — no API key, no Apify, no rate limits.
  Keyword search:  https://hn.algolia.com/api/v1/search_by_date?query=...&tags=story
  Story type feed: same endpoint with empty query, filtered by tag (ask_hn, show_hn, etc.)

Date filtering is done server-side via numericFilters (cheap — avoids over-fetching).
"""

import sys
import json
import re
import time
from datetime import datetime, timezone, timedelta
from typing import Optional, List
from urllib.parse import urlencode

import requests

ALGOLIA_BASE = "https://hn.algolia.com/api/v1"
HN_ITEM_URL = "https://news.ycombinator.com/item?id={}"
REQUEST_TIMEOUT = 15
MAX_PER_PAGE = 50  # Algolia max hitsPerPage

VALID_TAGS = {"story", "ask_hn", "show_hn", "job", "poll"}


def _build_url(query: str, tag: str, sort_by: str, page: int, hits_per_page: int, since_ts: Optional[int]) -> str:
    endpoint = "search" if sort_by == "relevance" else "search_by_date"
    params = {
        "query": query,
        "tags": tag,
        "hitsPerPage": hits_per_page,
        "page": page,
    }
    if since_ts:
        params["numericFilters"] = f"created_at_i>{since_ts}"
    return f"{ALGOLIA_BASE}/{endpoint}?{urlencode(params)}"


def _fetch_page(url: str) -> Optional[dict]:
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"  Request failed: {e}", file=sys.stderr)
        return None


def _parse_hit(hit: dict) -> dict:
    story_id = hit.get("objectID") or hit.get("story_id", "")
    return {
        "id": str(story_id),
        "title": hit.get("title", ""),
        "url": hit.get("url", ""),
        "hn_url": HN_ITEM_URL.format(story_id) if story_id else "",
        "author": hit.get("author", ""),
        "created_at": hit.get("created_at", ""),
        "points": hit.get("points") or 0,
        "num_comments": hit.get("num_comments") or 0,
    }


def scrape_hn(
    query: str = "",
    story_type: str = "story",
    sort_by: str = "date",
    days_back: int = 30,
    max_results: int = 30,
) -> dict:
    """
    Search Hacker News stories by keyword and/or type.

    Args:
        query:       Keyword(s) to search. Empty string fetches recent stories of the given type.
        story_type:  One of: story, ask_hn, show_hn, job, poll. Default: story.
        sort_by:     'date' (newest first) or 'relevance'. Default: date.
        days_back:   Only include stories from the last N days. 0 = no date filter.
        max_results: Cap on stories returned.

    Returns:
        dict with keys: query, story_type, stories, story_count, errors
    """
    errors = []

    tag = story_type if story_type in VALID_TAGS else "story"
    since_ts = None
    if days_back > 0:
        since_ts = int((datetime.now(timezone.utc) - timedelta(days=days_back)).timestamp())

    stories = []
    page = 0

    while len(stories) < max_results:
        batch_size = min(MAX_PER_PAGE, max_results - len(stories))
        url = _build_url(query, tag, sort_by, page, batch_size, since_ts)
        data = _fetch_page(url)

        if data is None:
            errors.append(f"Failed to fetch page {page}")
            break

        hits = data.get("hits", [])
        if not hits:
            break

        for hit in hits:
            stories.append(_parse_hit(hit))

        total_pages = data.get("nbPages", 1)
        page += 1
        if page >= total_pages:
            break

        # Small delay between pages to be polite
        if len(stories) < max_results:
            time.sleep(0.2)

    stories = stories[:max_results]

    return {
        "query": query,
        "story_type": tag,
        "sort_by": sort_by,
        "days_back": days_back,
        "stories": stories,
        "story_count": len(stories),
        "errors": errors,
    }


if __name__ == "__main__":
    input_file = sys.argv[1] if len(sys.argv) > 1 else "example_input.json"
    with open(input_file) as f:
        inp = json.load(f)

    result = scrape_hn(
        query=inp.get("query", ""),
        story_type=inp.get("story_type", "story"),
        sort_by=inp.get("sort_by", "date"),
        days_back=inp.get("days_back", 30),
        max_results=inp.get("max_results", 30),
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
