"""
LinkedIn Post Comment Scraper — extract comments from a LinkedIn post.

Actor: apimaestro/linkedin-post-comments-replies-engagements-scraper-no-cookies
Pricing: $1.2/1,000 comments (~$0.0012/comment). No login required.

Pagination: actor returns up to `limit` comments per page (max 100), sorted by
`sortOrder`. We request `limit=max_comments` and paginate via `page_number` only
if replies are filtered out and we still need more top-level comments — so we
never buy a full 100-comment page just to keep 20.

Input:  post_url, max_comments (default 20), include_replies (default False)
Output: dict with post_url, comments[], comment_count, errors
"""

import os
import sys
import re
import json
import logging
from contextlib import contextmanager

from dotenv import load_dotenv
from apify_client import ApifyClient

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
from scrapers._apify import dataset_items  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(__file__), "../../.env"))

ACTOR_ID = "apimaestro/linkedin-post-comments-replies-engagements-scraper-no-cookies"
PAGE_SIZE = 100  # Actor's max results per page (its `limit` field caps this)
MAX_PAGES = 5    # Safety cap so a reply-heavy post can't run up unbounded pages


# Silence Apify's client logger once at import (thread-safe). Per-call actor-run
# log streaming is disabled via logger=None.
logging.getLogger("apify_client").setLevel(logging.WARNING)


@contextmanager
def _suppress_apify_logs():
    """No-op, kept for call-site compatibility. A global stdout/stderr swap
    corrupts output when scrapers run on worker threads concurrently; log
    streaming is disabled at the source via ``.call(logger=None)`` instead."""
    yield


def _extract_post_id(post_url: str) -> str:
    """Extract numeric activity ID from a LinkedIn post URL."""
    match = re.search(r"activity-(\d+)", post_url)
    if match:
        return match.group(1)
    # Already a numeric ID
    if re.fullmatch(r"\d+", post_url.strip()):
        return post_url.strip()
    # Return full URL — actor accepts both
    return post_url


def _parse_comment(raw: dict) -> dict:
    author = raw.get("author") or {}
    posted_at = raw.get("posted_at") or {}
    stats = raw.get("stats") or {}
    reactions = stats.get("reactions") or {}

    return {
        "comment_id": raw.get("comment_id", ""),
        "comment_type": raw.get("comment_type", "comment"),  # "comment" or "reply"
        "text": raw.get("text", ""),
        "date": posted_at.get("date", ""),
        "is_edited": bool(raw.get("is_edited")),
        "is_pinned": bool(raw.get("is_pinned")),
        "comment_url": raw.get("comment_url", ""),
        "author": {
            "name": author.get("name", ""),
            "headline": author.get("headline", ""),
            "profile_url": author.get("profile_url", ""),
        },
        "reactions": {
            "total": stats.get("total_reactions", 0),
            "like": reactions.get("like", 0),
            "appreciation": reactions.get("appreciation", 0),
            "empathy": reactions.get("empathy", 0),
            "praise": reactions.get("praise", 0),
            "interest": reactions.get("interest", 0),
        },
        "reply_count": stats.get("comments", 0),
    }


def scrape_post_comments(
    post_url: str,
    max_comments: int = 20,
    include_replies: bool = False,
) -> dict:
    """
    Scrape comments from a LinkedIn post.

    Args:
        post_url:        Full LinkedIn post URL or numeric activity ID.
                         e.g. https://www.linkedin.com/posts/user_activity-1234567890-xxxx
        max_comments:    Max comments to return (client-side cap).
        include_replies: If True, includes reply comments. Default False — top-level only.

    Returns:
        dict with keys: post_url, comments, comment_count, errors
    """
    errors = []
    api_key = os.environ.get("APIFY_API_TOKEN")
    if not api_key:
        return {"post_url": post_url, "comments": [], "comment_count": 0, "errors": ["APIFY_API_TOKEN not set"]}

    client = ApifyClient(api_key)
    post_id = _extract_post_id(post_url)

    print(f"Fetching comments for: {post_url}", file=sys.stderr)

    # Ask the actor for only as many results per page as we still need, capped
    # at the actor's page size. The actor bills per returned comment, so the
    # default (limit=100) would buy a full page even when max_comments is 20.
    # Requesting `limit` cuts that overpay with no change to which comments we
    # keep — sortOrder "most recent" gives the same top-of-list selection.
    per_page = min(max_comments, PAGE_SIZE)

    comments = []
    seen_ids = set()
    page = 1
    while len(comments) < max_comments and page <= MAX_PAGES:
        run_input = {
            "postIds": [post_id],
            "page_number": page,   # actor's field is `page_number` (not `page`)
            "limit": per_page,
            "sortOrder": "most recent",
        }
        try:
            with _suppress_apify_logs():
                run = client.actor(ACTOR_ID).call(run_input=run_input, logger=None)
            items = dataset_items(client, run)
        except Exception as e:
            errors.append(f"Actor run failed (page {page}): {e}")
            break

        if not items:
            break

        for item in items:
            comment = _parse_comment(item)

            if comment["comment_id"] in seen_ids:
                continue
            seen_ids.add(comment["comment_id"])

            # Replies are billed but not filterable server-side, so we drop them
            # here. When excluding replies we may need a further page to reach
            # max_comments top-level comments — hence the loop.
            if not include_replies and comment["comment_type"] == "reply":
                continue

            comments.append(comment)
            if len(comments) >= max_comments:
                break

        # Fewer items than requested → last page reached.
        if len(items) < per_page:
            break
        page += 1

    return {
        "post_url": post_url,
        "comments": comments,
        "comment_count": len(comments),
        "errors": errors,
    }


if __name__ == "__main__":
    input_file = sys.argv[1] if len(sys.argv) > 1 else "example_input.json"
    with open(input_file) as f:
        inp = json.load(f)

    result = scrape_post_comments(
        post_url=inp["post_url"],
        max_comments=inp.get("max_comments", 20),
        include_replies=inp.get("include_replies", False),
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
