"""
LinkedIn Post Research Scraper
Search LinkedIn posts by keyword. Returns post content, author, and engagement stats.

Actor: harvestapi/linkedin-post-search (swapped 2026-07-09 from
apimaestro/linkedin-posts-search-scraper-no-cookies — $2/1k vs $5/1k).
No LinkedIn account or cookies required.
"""

import json
import os
import sys
import time
import contextlib
from typing import Optional, List

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "../../.env"))

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
from scrapers._harvest import parse_author, parse_stats, is_post_item, clean_url, _to_int  # noqa: E402

APIFY_TOKEN = os.getenv("APIFY_API_TOKEN")
ACTOR_ID = "harvestapi~linkedin-post-search"
BASE_URL = "https://api.apify.com/v2"

# Our public sort names → the actor's sortBy values
_SORT_MAP = {"date_posted": "date", "relevance": "relevance"}


@contextlib.contextmanager
def _suppress_apify_logs():
    # No-op, kept for call-site compatibility. This used to redirect sys.stderr
    # to an in-memory buffer, but a global stream swap corrupts output when
    # searches run concurrently (workflows now search several keywords in
    # parallel). These calls use raw `requests`, so there is no actor-log
    # streaming to suppress anyway.
    yield


def _run_actor(payload: dict) -> str:
    """Start an Apify actor run and return the run ID."""
    resp = requests.post(
        f"{BASE_URL}/acts/{ACTOR_ID}/runs",
        params={"token": APIFY_TOKEN},
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["data"]["id"]


def _wait_for_run(run_id: str, timeout: int = 120) -> dict:
    """Poll until the run finishes, then return the run record."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = requests.get(
            f"{BASE_URL}/actor-runs/{run_id}",
            params={"token": APIFY_TOKEN},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        if data["status"] in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
            return data
        time.sleep(5)
    raise TimeoutError(f"Actor run {run_id} did not finish within {timeout}s")


def _fetch_dataset(dataset_id: str, limit: int = 100) -> list:
    """Fetch items from an Apify dataset."""
    resp = requests.get(
        f"{BASE_URL}/datasets/{dataset_id}/items",
        params={"token": APIFY_TOKEN, "limit": limit},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _parse_post(raw: dict, keyword: str) -> dict:
    """Map a HarvestAPI post item to our search-post schema."""
    posted_at = raw.get("postedAt") or {}
    stats = parse_stats(raw)
    author = parse_author(raw.get("author") or {})

    return {
        "post_id": raw.get("id", ""),
        "post_url": clean_url(raw.get("linkedinUrl")),
        "text": raw.get("content", "") or "",
        "author": {
            "name": author["name"],
            "headline": author["headline"] or "",
            "profile_url": author["profile_url"],
        },
        "stats": {
            "reactions": stats["total_reactions"],
            "comments": stats["comments"],
            "shares": stats["reposts"],
        },
        "posted_at": posted_at.get("date", ""),
        "posted_at_timestamp": _to_int(posted_at.get("timestamp")),
        "hashtags": [w for w in (raw.get("content") or "").split() if w.startswith("#")],
        "content_type": raw.get("type", ""),
        "is_reshare": bool(raw.get("repostedBy")),
        "search_input": keyword,
    }


def search_linkedin_posts(
    keyword: str,
    sort: str = "date_posted",
    max_posts: int = 20,
    date_filter: Optional[str] = None,
) -> dict:
    """
    Search LinkedIn posts by keyword.

    Args:
        keyword:     Search term (e.g. "hiring SDR", "context graph", "#saas").
        sort:        "date_posted" (newest first) or "relevance".
        max_posts:   Max posts to return (each costs $0.002 on free tier).
        date_filter: Optional time window — one of '1h', '24h', 'week', 'month'.

    Returns:
        {
            "keyword": str,
            "sort": str,
            "posts": [...],
            "post_count": int,
            "total_available": int,
            "errors": [...]
        }
    """
    errors = []
    posts = []

    payload: dict = {
        "searchQueries": [keyword],
        "maxPosts": max_posts,
        "sortBy": _SORT_MAP.get(sort, "relevance"),
    }
    if date_filter:
        payload["postedLimit"] = date_filter

    try:
        with _suppress_apify_logs():
            run_id = _run_actor(payload)
            run = _wait_for_run(run_id)

        if run["status"] != "SUCCEEDED":
            errors.append(f"Actor run failed with status: {run['status']}")
        else:
            # `run` here is a raw REST dict from _wait_for_run, not an SDK Run object.
            dataset_id = run["defaultDatasetId"]
            raw_items = _fetch_dataset(dataset_id, limit=max_posts)

            # Deduplicate by post id (defensive — the actor shouldn't dupe)
            seen = set()
            for item in raw_items:
                if not is_post_item(item):
                    msg = item.get("error") or item.get("message")
                    if msg:
                        errors.append(f"Actor message: {msg}")
                    continue
                pid = item.get("id", "")
                if pid and pid not in seen:
                    seen.add(pid)
                    posts.append(_parse_post(item, keyword))
                    if len(posts) >= max_posts:
                        break

    except Exception as e:
        errors.append(str(e))

    return {
        "keyword": keyword,
        "sort": sort,
        "posts": posts,
        "post_count": len(posts),
        "total_available": len(posts),
        "errors": errors,
    }


def search_linkedin_posts_batch(
    keywords: List[str],
    sort: str = "date_posted",
    max_posts: int = 20,
    date_filter: Optional[str] = None,
) -> List[dict]:
    """
    Run search_linkedin_posts for multiple keywords sequentially.
    Adds a 2s delay between calls to avoid hammering the actor.
    """
    results = []
    for i, kw in enumerate(keywords):
        if i > 0:
            time.sleep(2)
        results.append(search_linkedin_posts(kw, sort=sort, max_posts=max_posts, date_filter=date_filter))
    return results


if __name__ == "__main__":
    input_path = sys.argv[1] if len(sys.argv) > 1 else "example_input.json"
    with open(input_path) as f:
        inp = json.load(f)

    result = search_linkedin_posts(
        keyword=inp["keyword"],
        sort=inp.get("sort", "date_posted"),
        max_posts=inp.get("max_posts", 20),
        date_filter=inp.get("date_filter"),
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
