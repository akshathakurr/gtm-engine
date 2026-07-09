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
from scrapers._harvest import parse_post, is_post_item  # noqa: E402

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


# Swapped 2026-07-09: apimaestro/linkedin-profile-posts ($5/1k) →
# harvestapi/linkedin-profile-posts ($2/1k). Same output contract; the actor
# handles limits and date cutoffs server-side, so no pagination loop is needed.
ACTOR_ID = "harvestapi/linkedin-profile-posts"


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

    # Resolve cutoff datetime (UTC) — passed server-side AND enforced locally.
    cutoff_dt: Optional[datetime] = None
    if days_back is not None:
        cutoff_dt = datetime.now(timezone.utc) - timedelta(days=days_back)
    elif since_date is not None:
        cutoff_dt = datetime.fromisoformat(since_date).replace(tzinfo=timezone.utc)

    client = ApifyClient(api_token)

    username = _extract_username(profile_url)
    print(f"Fetching posts for {username}...")

    actor_input = {
        "targetUrls": [profile_url],
        "maxPosts": max_posts,
        "includeReposts": False,  # matches the old behavior of dropping reposts
    }
    if cutoff_dt is not None:
        actor_input["postedLimitDate"] = cutoff_dt.strftime("%Y-%m-%d")

    errors: list = []
    try:
        run = client.actor(ACTOR_ID).call(run_input=actor_input, logger=None)
        items = dataset_items(client, run)
    except ApifyRunError as e:
        return {"profile_url": profile_url, "total": 0, "posts": [], "errors": [str(e)]}

    posts = []
    for item in items:
        if not is_post_item(item):
            msg = item.get("error") or item.get("message")
            if msg:
                errors.append(f"Actor message: {msg}")
            continue
        post = parse_post(item)
        if post["post_type"] == "repost":
            continue
        # Local cutoff guard — the server-side date limit is authoritative, but
        # keep the old exact-cutoff behavior for same-day precision.
        if cutoff_dt is not None and post["timestamp_ms"]:
            post_dt = datetime.fromtimestamp(post["timestamp_ms"] / 1000, tz=timezone.utc)
            if post_dt < cutoff_dt:
                continue
        posts.append(post)

    posts.sort(key=lambda p: p.get("timestamp_ms") or 0, reverse=True)
    posts = posts[:max_posts]

    return {
        "profile_url": profile_url,
        "total": len(posts),
        "posts": posts,
        "errors": errors,
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
