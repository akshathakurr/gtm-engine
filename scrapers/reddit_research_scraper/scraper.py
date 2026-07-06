"""
Reddit Research Scraper — search Reddit posts by keyword, subreddit, or both.

Uses Reddit's free public JSON API (no Apify, no API key).

NOTE: Reddit IP-blocks datacenter/cloud ranges (HTTP 403), so this often returns
only errors from CI or a server. When that happens, fall back to the Apify
`trudax/reddit-scraper-lite` actor, which uses residential proxies. Errors are
returned in the result's `errors` list rather than raised.

Input:  query, subreddit (optional), sort, time_filter, max_posts
Output: list of posts with title, text, author, subreddit, score, comments, url, date
"""

import os
import sys
import json
import time
from datetime import datetime, timezone
from typing import Optional, List

import requests

REDDIT_API_BASE = "https://www.reddit.com"
USER_AGENT = "GTMEngine/1.0"
PAGE_SIZE = 100  # Reddit max per request

# Fallback when Reddit IP-blocks the free JSON API (HTTP 403 from datacenter/
# cloud ranges). Field names confirmed from raw_sample_apify.json.
APIFY_FALLBACK_ACTOR = "trudax/reddit-scraper-lite"
_APIFY_SORT_ALLOWED = {"relevance", "hot", "top", "new", "rising", "comments"}


def _fetch_page(
    query: str,
    subreddit: Optional[str],
    sort: str,
    time_filter: str,
    after: Optional[str],
    limit: int,
) -> dict:
    if subreddit:
        url = f"{REDDIT_API_BASE}/r/{subreddit}/search.json"
        params = {"q": query, "restrict_sr": "true"}
    else:
        url = f"{REDDIT_API_BASE}/search.json"
        params = {"q": query}

    params.update({
        "sort": sort,
        "t": time_filter,
        "limit": min(limit, PAGE_SIZE),
    })
    if after:
        params["after"] = after

    resp = requests.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _parse_post(raw: dict) -> dict:
    data = raw.get("data", {})
    created_utc = data.get("created_utc", 0)
    created_dt = datetime.fromtimestamp(created_utc, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC") if created_utc else ""

    return {
        "id": data.get("name", ""),
        "title": data.get("title", ""),
        "text": data.get("selftext", ""),
        "url": f"https://www.reddit.com{data.get('permalink', '')}",
        "external_url": data.get("url", "") if not data.get("is_self") else "",
        "author": data.get("author", ""),
        "subreddit": data.get("subreddit", ""),
        "score": data.get("score", 0),
        "upvote_ratio": data.get("upvote_ratio", 0),
        "num_comments": data.get("num_comments", 0),
        "created_at": created_dt,
        "flair": data.get("link_flair_text", ""),
        "is_self": bool(data.get("is_self")),
    }


def _search_via_apify(
    query: str,
    subreddit: Optional[str],
    sort: str,
    time_filter: str,
    max_posts: int,
) -> list:
    """Search via the Apify fallback actor. Returns parsed posts; raises on failure."""
    from apify_client import ApifyClient

    _repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if _repo_root not in sys.path:
        sys.path.insert(0, _repo_root)
    from scrapers._apify import dataset_items

    api_token = os.environ.get("APIFY_API_TOKEN")
    if not api_token:
        raise EnvironmentError("APIFY_API_TOKEN is not set (needed for the Reddit fallback).")

    actor_input = {
        "searches": [query],
        "searchPosts": True,
        "searchComments": False,
        "searchCommunities": False,
        "searchUsers": False,
        "skipComments": True,
        # NOTE: do not set includeMediaLinks — it makes the actor return 0 items
        # (verified 2026-07-06). Engagement counts are unavailable in fallback mode.
        "sort": sort if sort in _APIFY_SORT_ALLOWED else "new",
        "maxItems": max_posts,
        "maxPostCount": max_posts,
        "proxy": {"useApifyProxy": True, "apifyProxyGroups": ["RESIDENTIAL"]},
    }
    if subreddit:
        actor_input["searchCommunityName"] = subreddit
    if time_filter and time_filter != "all":
        actor_input["time"] = time_filter

    client = ApifyClient(api_token)
    run = client.actor(APIFY_FALLBACK_ACTOR).call(run_input=actor_input, logger=None)
    items = dataset_items(client, run)

    posts = []
    for item in items:
        if item.get("dataType") not in (None, "post"):
            continue
        created_raw = item.get("createdAt", "")
        created_at = ""
        if created_raw:
            try:
                dt = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
                created_at = dt.strftime("%Y-%m-%d %H:%M:%S UTC")
            except ValueError:
                created_at = created_raw
        posts.append({
            "id": item.get("id", ""),
            "title": item.get("title", ""),
            "text": item.get("body", ""),
            "url": item.get("url", ""),
            "external_url": "",
            "author": item.get("username", ""),
            "subreddit": item.get("parsedCommunityName", ""),
            "score": item.get("upVotes", 0) or 0,
            "upvote_ratio": item.get("upVoteRatio", 0) or 0,
            "num_comments": item.get("numberOfComments", 0) or 0,
            "created_at": created_at,
            "flair": "",
            "is_self": bool(item.get("body")),
        })
    return posts[:max_posts]


def search_reddit(
    query: str,
    subreddit: Optional[str] = None,
    sort: str = "new",
    time_filter: str = "week",
    max_posts: int = 25,
) -> dict:
    """
    Search Reddit posts by keyword, optionally scoped to a subreddit.

    Args:
        query:       Search query. Supports Reddit operators: site:, author:, subreddit:.
        subreddit:   Scope search to a specific subreddit. e.g. "SaaS", "entrepreneur".
                     None = search all of Reddit.
        sort:        "new", "top", "relevance", "hot", "comments". Default "new".
        time_filter: "hour", "day", "week", "month", "year", "all". Default "week".
        max_posts:   Max posts to return.

    Returns:
        dict with keys: query, subreddit, sort, time_filter, posts, post_count, errors
    """
    errors = []
    print(
        f"Searching Reddit: '{query}'"
        + (f" in r/{subreddit}" if subreddit else "")
        + f" (sort={sort}, t={time_filter})",
        file=sys.stderr,
    )

    all_posts = []
    after = None
    seen_ids = set()

    while len(all_posts) < max_posts:
        remaining = max_posts - len(all_posts)
        try:
            data = _fetch_page(query, subreddit, sort, time_filter, after, min(remaining, PAGE_SIZE))
        except Exception as e:
            print(f"  Free JSON API failed ({e}); falling back to Apify {APIFY_FALLBACK_ACTOR}", file=sys.stderr)
            try:
                fallback_posts = _search_via_apify(query, subreddit, sort, time_filter, max_posts)
                for post in fallback_posts:
                    if post["id"] not in seen_ids:
                        seen_ids.add(post["id"])
                        all_posts.append(post)
            except Exception as fe:
                errors.append(f"Fetch failed: {e}")
                errors.append(f"Apify fallback failed: {fe}")
            break

        children = data.get("data", {}).get("children", [])
        after = data.get("data", {}).get("after")

        for child in children:
            post = _parse_post(child)
            if post["id"] in seen_ids:
                continue
            seen_ids.add(post["id"])
            all_posts.append(post)
            if len(all_posts) >= max_posts:
                break

        # No more pages
        if not after or len(children) < PAGE_SIZE:
            break

        time.sleep(0.5)  # polite rate limiting

    return {
        "query": query,
        "subreddit": subreddit,
        "sort": sort,
        "time_filter": time_filter,
        "posts": all_posts,
        "post_count": len(all_posts),
        "errors": errors,
    }


def search_reddit_batch(
    queries: List[str],
    subreddit: Optional[str] = None,
    sort: str = "new",
    time_filter: str = "week",
    max_posts: int = 25,
) -> List[dict]:
    """
    Search Reddit for multiple queries. Use instead of looping over search_reddit().

    Returns list of result dicts, one per query, in input order.
    """
    results = []
    for i, query in enumerate(queries):
        if i > 0:
            time.sleep(1)  # polite delay between queries
        results.append(search_reddit(query, subreddit, sort, time_filter, max_posts))
    return results


if __name__ == "__main__":
    input_file = sys.argv[1] if len(sys.argv) > 1 else "example_input.json"
    with open(input_file) as f:
        inp = json.load(f)

    result = search_reddit(
        query=inp["query"],
        subreddit=inp.get("subreddit"),
        sort=inp.get("sort", "new"),
        time_filter=inp.get("time_filter", "week"),
        max_posts=inp.get("max_posts", 25),
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
