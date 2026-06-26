"""
LinkedIn Post Research Scraper
Search LinkedIn posts by keyword. Returns post content, author, and engagement stats.

Actor: apimaestro/linkedin-posts-search-scraper-no-cookies
Cost: $0.005/post (PAY_PER_EVENT, free tier)
No LinkedIn account or cookies required.
"""

import json
import os
import sys
import time
import contextlib
from typing import Optional, List
from datetime import datetime

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "../../.env"))
APIFY_TOKEN = os.getenv("APIFY_API_TOKEN")
ACTOR_ID = "apimaestro~linkedin-posts-search-scraper-no-cookies"
BASE_URL = "https://api.apify.com/v2"


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


def _parse_post(raw: dict) -> dict:
    """Map raw actor output fields to our schema."""
    author = raw.get("author") or {}
    stats = raw.get("stats") or {}
    posted_at = raw.get("posted_at") or {}
    content = raw.get("content") or {}

    return {
        "post_id": raw.get("activity_id", ""),
        "post_url": raw.get("post_url", ""),
        "text": raw.get("text", ""),
        "author": {
            "name": author.get("name", ""),
            "headline": author.get("headline", ""),
            "profile_url": author.get("profile_url", ""),
        },
        "stats": {
            "reactions": stats.get("total_reactions", 0),
            "comments": stats.get("comments", 0),
            "shares": stats.get("shares", 0),
        },
        "posted_at": posted_at.get("date", ""),
        "posted_at_timestamp": posted_at.get("timestamp"),
        "hashtags": raw.get("hashtags", []),
        "content_type": content.get("type", ""),
        "is_reshare": raw.get("is_reshare", False),
        "search_input": raw.get("search_input", ""),
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
        max_posts:   Max posts to return (each costs $0.005 on free tier).
        date_filter: Optional time window — leave empty for all time.
                     The actor doesn't document valid values; omit unless you
                     know the accepted string.

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
        "keyword": keyword,
        "sort_type": sort,
        "page_number": 1,
        "maxPosts": max_posts,
    }
    if date_filter:
        payload["date_filter"] = date_filter

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

            # Extract total_available from the first item's metadata
            total_available = 0
            if raw_items:
                meta = raw_items[0].get("metadata") or {}
                total_available = meta.get("total_count", 0)

            # Deduplicate by activity_id (actor may return dupes across pages)
            seen = set()
            for item in raw_items:
                aid = item.get("activity_id", "")
                if aid and aid not in seen:
                    seen.add(aid)
                    posts.append(_parse_post(item))
                    if len(posts) >= max_posts:
                        break

    except Exception as e:
        errors.append(str(e))

    return {
        "keyword": keyword,
        "sort": sort,
        "posts": posts,
        "post_count": len(posts),
        "total_available": total_available if not errors else 0,
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
