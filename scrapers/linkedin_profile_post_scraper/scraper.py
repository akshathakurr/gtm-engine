import os
import sys
import json
import logging
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from typing import Optional
from apify_client import ApifyClient

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
from scrapers._apify import dataset_items, ApifyRunError  # noqa: E402

# Silence Apify's client logger once at import (thread-safe — happens before any
# concurrent use). Per-call actor-run log streaming is disabled via logger=None.
logging.getLogger("apify_client").setLevel(logging.WARNING)


@contextmanager
def _suppress_apify_logs():
    """No-op, kept for call-site compatibility.

    This used to redirect sys.stdout/sys.stderr to /dev/null, but a global stream
    swap corrupts output when scrapers run concurrently (workflows now scrape
    several profiles in parallel). Streaming is instead disabled at the source
    via ``.call(logger=None)``, so no suppression context is needed.
    """
    yield

ACTOR_ID = "apimaestro/linkedin-profile-posts"
POSTS_PER_PAGE = 100


DEFAULT_MAX_POSTS = 15
DEFAULT_DAYS_BACK = 90


def scrape_linkedin_profile_posts(
    profile_url: str,
    max_posts: int = DEFAULT_MAX_POSTS,
    days_back: Optional[int] = DEFAULT_DAYS_BACK,
    since_date: Optional[str] = None,
) -> dict:
    """
    Fetches posts from a LinkedIn user's profile.

    Defaults to the last 90 days, capped at 15 posts. Both limits apply together —
    you get at most 15 posts from the last 90 days. Override either explicitly.

    Args:
        profile_url:  LinkedIn profile URL (e.g. https://www.linkedin.com/in/username/)
        max_posts:    Maximum number of posts to return (default 15).
        days_back:    Return only posts from the last N days (default 90).
                      Pass None to remove the date limit entirely.
        since_date:   Return only posts on or after this date (YYYY-MM-DD).
                      Used only when days_back is None.

    Returns:
        dict with keys: profile_url, total, posts, errors
        Posts are ordered newest → oldest.
    """
    api_token = os.environ.get("APIFY_API_TOKEN")
    if not api_token:
        raise EnvironmentError("APIFY_API_TOKEN environment variable is not set.")

    # Resolve cutoff datetime (UTC)
    cutoff_dt: Optional[datetime] = None
    if days_back is not None:
        cutoff_dt = datetime.now(timezone.utc) - timedelta(days=days_back)
    elif since_date is not None:
        cutoff_dt = datetime.fromisoformat(since_date).replace(tzinfo=timezone.utc)

    client = ApifyClient(api_token)

    username = _extract_username(profile_url)

    all_raw: list = []
    errors: list = []
    pagination_token: Optional[str] = None
    seen_urns: set = set()
    page = 1

    print(f"Fetching posts for {username}...")

    while len(all_raw) < max_posts:
        # Request only as many posts as we still need — minimises data cost
        remaining = max_posts - len(all_raw)
        actor_input = {
            "profileUrls": [{"url": profile_url}],
            "username": username,
            "limit": min(remaining, POSTS_PER_PAGE),
            "page_number": page,
        }
        if pagination_token:
            actor_input["pagination_token"] = pagination_token

        run = client.actor(ACTOR_ID).call(run_input=actor_input, logger=None)
        try:
            items = dataset_items(client, run)
        except ApifyRunError as e:
            errors.append(f"Actor run failed (page {page}): {e}")
            break

        if not items:
            break

        new_items = []
        stop_early = False

        for item in items:
            urn = item.get("full_urn")
            if not urn or urn in seen_urns:
                continue
            seen_urns.add(urn)

            if item.get("post_type") == "repost":
                continue

            # Stop as soon as we go past the cutoff — posts are newest→oldest
            if cutoff_dt is not None:
                ts_ms = (item.get("posted_at") or {}).get("timestamp")
                if ts_ms is not None:
                    post_dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
                    if post_dt < cutoff_dt:
                        stop_early = True
                        break

            new_items.append(item)

        all_raw.extend(new_items)

        if stop_early:
            break

        # Check if there are more pages
        last_token = items[-1].get("pagination_token") if items else None
        if not last_token or last_token == pagination_token:
            break

        pagination_token = last_token
        page += 1

    # Always cap at max_posts
    all_raw = all_raw[:max_posts]

    posts = [_parse_post(item) for item in all_raw]

    return {
        "profile_url": profile_url,
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

    post = {
        "urn": item.get("full_urn"),
        "url": item.get("url"),
        "post_type": item.get("post_type"),
        "posted_at": posted_at.get("date"),
        "timestamp_ms": posted_at.get("timestamp"),
        "text": item.get("text"),
        "author": _parse_author(item.get("author") or {}),
        "stats": _parse_stats(item.get("stats") or {}),
        "media": _parse_media(media),
        "reshared_post": _parse_reshared(reshared) if reshared else None,
    }
    return post


def _parse_author(author: dict) -> dict:
    return {
        "name": f"{author.get('first_name', '')} {author.get('last_name', '')}".strip(),
        "headline": author.get("headline"),
        "username": author.get("username"),
        "profile_url": author.get("profile_url"),
    }


def _parse_stats(stats: dict) -> dict:
    return {
        "total_reactions": stats.get("total_reactions", 0),
        "likes": stats.get("like", 0),
        "comments": stats.get("comments", 0),
        "reposts": stats.get("reposts", 0),
    }


def _parse_media(media: Optional[dict]) -> Optional[dict]:
    if not media:
        return None
    result = {"type": media.get("type"), "url": media.get("url")}
    if media.get("thumbnail"):
        result["thumbnail"] = media["thumbnail"]
    if media.get("images"):
        result["images"] = [
            {"url": img.get("url"), "width": img.get("width"), "height": img.get("height")}
            for img in media["images"]
        ]
    return result


def _parse_reshared(reshared: dict) -> dict:
    posted_at = reshared.get("posted_at") or {}
    return {
        "urn": (reshared.get("urn") or {}).get("activity_urn") or (reshared.get("urn") or {}).get("ugcPost_urn"),
        "url": reshared.get("url"),
        "post_type": reshared.get("post_type"),
        "posted_at": posted_at.get("date"),
        "text": reshared.get("text"),
        "author": _parse_author(reshared.get("author") or {}),
        "media": _parse_media(reshared.get("media")),
    }


def _extract_username(profile_url: str) -> str:
    """Extracts the LinkedIn username from a profile URL."""
    parts = profile_url.rstrip("/").split("/")
    try:
        in_idx = parts.index("in")
        return parts[in_idx + 1]
    except (ValueError, IndexError):
        return profile_url


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    input_file = sys.argv[1] if len(sys.argv) > 1 else "example_input.json"

    with open(input_file) as f:
        input_data = json.load(f)

    result = scrape_linkedin_profile_posts(
        profile_url=input_data["profile_url"],
        max_posts=input_data.get("max_posts", DEFAULT_MAX_POSTS),
        days_back=input_data.get("days_back", DEFAULT_DAYS_BACK),
        since_date=input_data.get("since_date"),
    )

    print(json.dumps(result, indent=2, ensure_ascii=False))
