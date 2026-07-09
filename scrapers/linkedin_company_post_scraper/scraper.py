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
from scrapers._harvest import parse_post, is_post_item, clean_url  # noqa: E402

# Swapped 2026-07-09: apimaestro/linkedin-company-posts ($5/1k) →
# harvestapi/linkedin-company-posts ($2/1k). Same output contract; limits are
# handled server-side, so no pagination loop is needed.
ACTOR_ID = "harvestapi/linkedin-company-posts"

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
    print(f"Fetching posts for {company_slug}...")

    actor_input = {
        "targetUrls": [company_url],
        "maxPosts": max_posts,
    }

    errors: list = []
    try:
        with _suppress_apify_logs():
            run = client.actor(ACTOR_ID).call(run_input=actor_input, logger=None)
        items = dataset_items(client, run)
    except ApifyRunError as e:
        return {"company_url": company_url, "total": 0, "posts": [], "errors": [str(e)]}

    posts = []
    for item in items:
        if not is_post_item(item):
            msg = item.get("error") or item.get("message")
            if msg:
                errors.append(f"Actor message: {msg}")
            continue
        posts.append(_to_company_post(parse_post(item)))

    posts.sort(key=lambda p: p.get("timestamp_ms") or 0, reverse=True)
    posts = posts[:max_posts]

    return {
        "company_url": company_url,
        "total": len(posts),
        "posts": posts,
        "errors": errors,
    }


def _to_company_post(post: dict) -> dict:
    """Company posts keep the shared shape but use the company author contract
    ({name, username, url}) the old scraper exposed."""
    author = post.pop("author", {}) or {}
    post["author"] = {
        "name": author.get("name"),
        "username": author.get("username"),
        "url": clean_url(author.get("profile_url")),
    }
    return post


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
        actor_input = {"targetUrls": [company_url], "maxPosts": 1}
        print(f"Discovery call for {company_url}...")
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
