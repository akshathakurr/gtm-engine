"""
LinkedIn Post Comment Scraper — extract comments from a LinkedIn post.

Actor: apimaestro/linkedin-post-comments-replies-engagements-scraper-no-cookies
Pricing: $1.2/1,000 comments (~$0.0012/comment). No login required.

Pagination: actor returns 100 comments per page. Use `page` param to paginate.

Input:  post_url, max_comments (default 20), include_replies (default False)
Output: dict with post_url, comments[], comment_count, errors
"""

import os
import sys
import re
import json
from typing import Optional, List
from contextlib import contextmanager

from dotenv import load_dotenv
from apify_client import ApifyClient

load_dotenv(os.path.join(os.path.dirname(__file__), "../../.env"))

ACTOR_ID = "apimaestro/linkedin-post-comments-replies-engagements-scraper-no-cookies"
PAGE_SIZE = 100  # Actor returns up to 100 comments per page


@contextmanager
def _suppress_apify_logs():
    with open(os.devnull, "w") as devnull:
        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout = devnull
        sys.stderr = devnull
        try:
            yield
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr


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

    # Paginate until we have enough comments
    all_raw = []
    page = 1
    while len(all_raw) < max_comments:
        run_input = {"postIds": [post_id], "page": page}
        try:
            with _suppress_apify_logs():
                run = client.actor(ACTOR_ID).call(run_input=run_input)
            items = list(client.dataset(run["defaultDatasetId"]).iterate_items())
        except Exception as e:
            errors.append(f"Actor run failed (page {page}): {e}")
            break

        if not items:
            break

        all_raw.extend(items)

        # If fewer than PAGE_SIZE returned, no more pages
        if len(items) < PAGE_SIZE:
            break
        page += 1

    # Parse and filter
    comments = []
    seen_ids = set()
    for item in all_raw:
        comment = _parse_comment(item)

        if comment["comment_id"] in seen_ids:
            continue
        seen_ids.add(comment["comment_id"])

        if not include_replies and comment["comment_type"] == "reply":
            continue

        comments.append(comment)

        if len(comments) >= max_comments:
            break

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
