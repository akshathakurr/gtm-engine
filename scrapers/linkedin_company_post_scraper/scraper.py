import os
import sys
import json
import logging
from contextlib import contextmanager
from typing import Optional
from apify_client import ApifyClient

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
from scrapers._apify import dataset_items, ApifyRunError  # noqa: E402

# ⚠️  FIELD NAME WARNING
# This scraper was built without a live discovery call (Apify monthly limit was exceeded).
# Field names below are based on apimaestro's profile-post actor conventions.
# When Apify credits are restored:
#   1. Run: python3 scraper.py --discover
#   2. Inspect raw_sample.json
#   3. Correct any field names that differ from the assumptions here.

ACTOR_ID = "apimaestro/linkedin-company-posts"
POSTS_PER_PAGE = 100

DEFAULT_MAX_POSTS = 10


# Silence Apify's client logger once at import (thread-safe). Per-call actor-run
# log streaming is disabled via logger=None.
logging.getLogger("apify_client").setLevel(logging.WARNING)


@contextmanager
def _suppress_apify_logs():
    """No-op, kept for call-site compatibility. A global stdout/stderr swap
    corrupts output when scrapers run on worker threads concurrently; log
    streaming is disabled at the source via ``.call(logger=None)`` instead."""
    yield


def scrape_linkedin_company_posts(
    company_url: str,
    max_posts: int = DEFAULT_MAX_POSTS,
) -> dict:
    """
    Fetches recent posts from a LinkedIn company page.

    Returns the latest N posts, ordered newest → oldest, with raw content intact.

    Args:
        company_url:  LinkedIn company page URL (e.g. https://www.linkedin.com/company/anthropic/)
        max_posts:    Maximum number of posts to return (default 10).

    Returns:
        dict with keys: company_url, total, posts, errors
        Posts are ordered newest → oldest.
    """
    api_token = os.environ.get("APIFY_API_TOKEN")
    if not api_token:
        raise EnvironmentError("APIFY_API_TOKEN environment variable is not set.")

    client = ApifyClient(api_token)

    company_slug = _extract_slug(company_url)

    all_raw: list = []
    errors: list = []
    pagination_token: Optional[str] = None
    seen_urns: set = set()
    page = 1

    print(f"Fetching posts for {company_slug}...")

    while len(all_raw) < max_posts:
        remaining = max_posts - len(all_raw)
        actor_input = {
            "company_name": company_slug,
            "sort": "recent",
            "limit": min(remaining, POSTS_PER_PAGE),
            "page_number": page,
        }
        if pagination_token:
            actor_input["pagination_token"] = pagination_token

        with _suppress_apify_logs():
            run = client.actor(ACTOR_ID).call(run_input=actor_input, logger=None)
        try:
            items = dataset_items(client, run)
        except ApifyRunError as e:
            errors.append(f"Actor run failed (page {page}): {e}")
            break

        if not items:
            break

        new_items = []
        for item in items:
            urn = item.get("full_urn") or item.get("urn")
            if not urn or urn in seen_urns:
                continue
            seen_urns.add(urn)
            new_items.append(item)

        all_raw.extend(new_items)

        last_token = items[-1].get("pagination_token") if items else None
        if not last_token or last_token == pagination_token:
            break

        pagination_token = last_token
        page += 1

    all_raw = all_raw[:max_posts]

    posts = [_parse_post(item) for item in all_raw]

    return {
        "company_url": company_url,
        "total": len(posts),
        "posts": posts,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _parse_post(item: dict) -> dict:
    posted_at = item.get("posted_at") or {}
    media = item.get("media")
    reshared = item.get("reshared_post")

    return {
        "urn": item.get("full_urn"),
        "url": item.get("post_url"),
        "post_type": item.get("post_type"),
        "posted_at": posted_at.get("date") if isinstance(posted_at, dict) else posted_at,
        "timestamp_ms": posted_at.get("timestamp") if isinstance(posted_at, dict) else None,
        "text": item.get("text"),
        "author": _parse_author(item.get("author") or {}),
        "stats": _parse_stats(item.get("stats") or {}),
        "media": _parse_media(media),
        "reshared_post": _parse_reshared(reshared) if reshared else None,
    }


def _parse_author(author: dict) -> dict:
    # Company author has different fields than a person author.
    # Try both shapes — company shape first, fall back to person shape.
    name = author.get("name")
    url = author.get("company_url")
    # Extract slug from company_url (e.g. https://www.linkedin.com/company/google/posts → google)
    username = None
    if url:
        parts = url.rstrip("/").replace("/posts", "").split("/")
        try:
            idx = parts.index("company")
            username = parts[idx + 1]
        except (ValueError, IndexError):
            pass
    return {"name": name, "username": username, "url": url}


def _parse_stats(stats: dict) -> dict:
    return {
        "total_reactions": stats.get("total_reactions", 0),
        "likes": stats.get("like", 0) or stats.get("likes", 0),
        "comments": stats.get("comments", 0),
        "reposts": stats.get("reposts", 0) or stats.get("shares", 0),
    }


def _parse_media(media: Optional[dict]) -> Optional[dict]:
    if not media or not media.get("type"):
        return None
    result = {"type": media.get("type"), "url": media.get("url"), "thumbnail": media.get("thumbnail")}
    if media.get("items"):
        result["images"] = [
            {"url": img.get("url"), "width": img.get("width"), "height": img.get("height")}
            for img in media["items"]
        ]
    return result


def _parse_reshared(reshared: dict) -> dict:
    posted_at = reshared.get("posted_at") or {}
    urn_field = reshared.get("urn")
    if isinstance(urn_field, dict):
        urn = urn_field.get("activity_urn") or urn_field.get("ugcPost_urn")
    else:
        urn = urn_field
    return {
        "urn": urn,
        "url": reshared.get("url"),
        "post_type": reshared.get("post_type"),
        "posted_at": posted_at.get("date") if isinstance(posted_at, dict) else posted_at,
        "text": reshared.get("text"),
        "author": _parse_author(reshared.get("author") or {}),
        "media": _parse_media(reshared.get("media")),
    }


def _extract_slug(company_url: str) -> str:
    """Extracts the company slug from a LinkedIn company URL."""
    parts = company_url.rstrip("/").split("/")
    try:
        idx = parts.index("company")
        return parts[idx + 1]
    except (ValueError, IndexError):
        return company_url


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Discovery mode: run 1 post, dump raw item to raw_sample.json
    if len(sys.argv) > 1 and sys.argv[1] == "--discover":
        company_url = sys.argv[2] if len(sys.argv) > 2 else "https://www.linkedin.com/company/anthropic/"
        api_token = os.environ.get("APIFY_API_TOKEN")
        if not api_token:
            raise EnvironmentError("APIFY_API_TOKEN environment variable is not set.")
        client = ApifyClient(api_token)
        slug = _extract_slug(company_url)
        actor_input = {
            "company_name": slug,
            "sort": "recent",
            "limit": 1,
            "page_number": 1,
        }
        print(f"Discovery call for {slug}...")
        with _suppress_apify_logs():
            run = client.actor(ACTOR_ID).call(run_input=actor_input, logger=None)
        items = dataset_items(client, run)
        output_path = os.path.join(os.path.dirname(__file__), "raw_sample.json")
        with open(output_path, "w") as f:
            json.dump(items, f, indent=2, ensure_ascii=False)
        print(f"Saved {len(items)} raw item(s) to raw_sample.json")
        sys.exit(0)

    input_file = sys.argv[1] if len(sys.argv) > 1 else "example_input.json"
    with open(input_file) as f:
        input_data = json.load(f)

    result = scrape_linkedin_company_posts(
        company_url=input_data["company_url"],
        max_posts=input_data.get("max_posts", DEFAULT_MAX_POSTS),
    )

    print(json.dumps(result, indent=2, ensure_ascii=False))
