"""
Twitter Research Scraper — search tweets by keyword, topic, or brand.

Actor: kaitoeasyapi/twitter-x-data-tweet-scraper-pay-per-result-cheapest
  (~$0.18-0.25 per 1,000 tweets). No Twitter/X login or cookies required — uses
  public guest tokens, so it's safe to ship in an OSS tool. Works on the Apify
  free plan. Replaced altimis/scweet, which demanded full X account access.

Input: search_query, max_tweets (default 20), days_back (default 7), include_replies (default False)
Output: list of tweets with author info, text, engagement metrics, tweet URL

Date filtering: appended to the query as Twitter's native `since:`/`until:`
operators (YYYY-MM-DD) — applied server-side. Reliable and no over-fetching.
"""

import os
import sys
import json
from datetime import datetime, timezone, timedelta
from typing import Optional, List
from contextlib import contextmanager

from dotenv import load_dotenv
from apify_client import ApifyClient

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
from scrapers._apify import dataset_items  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(__file__), "../../.env"))

ACTOR_ID = "kaitoeasyapi/twitter-x-data-tweet-scraper-pay-per-result-cheapest"
TWITTER_DATE_FORMAT = "%a %b %d %H:%M:%S +0000 %Y"  # "Sun Apr 05 15:36:19 +0000 2026"
MIN_ITEMS = 20  # actor's minimum billable items per run
COST_PER_TWEET_USD = 0.00025  # ~$0.25 / 1,000 tweets


import logging

# Silence Apify's client logger once at import (thread-safe). Per-call actor-run
# log streaming is disabled via logger=None.
logging.getLogger("apify_client").setLevel(logging.WARNING)


@contextmanager
def _suppress_apify_logs():
    """No-op, kept for call-site compatibility.

    Previously swapped sys.stdout/sys.stderr to /dev/null, but a global stream
    swap corrupts output when this scraper runs alongside others on worker
    threads. Streaming is disabled at the source via ``.call(logger=None)``.
    """
    yield


def _parse_tweet(raw: dict) -> dict:
    """Parse a raw kaitoeasyapi tweet item into clean output fields."""
    author = raw.get("author") or {}
    screen_name = author.get("userName", "")

    # tweet url uses x.com — normalize to twitter.com for consistency
    tweet_url = (raw.get("twitterUrl") or raw.get("url") or "").replace("x.com", "twitter.com")
    profile_url = author.get("twitterUrl") or (f"https://twitter.com/{screen_name}" if screen_name else "")

    # Hashtags and mentions live in entities
    entities = raw.get("entities") or {}
    raw_hashtags = entities.get("hashtags") or []
    raw_mentions = entities.get("user_mentions") or []
    hashtags = [h.get("text", h) if isinstance(h, dict) else h for h in raw_hashtags if h]
    mentions = [m.get("screen_name", m) if isinstance(m, dict) else m for m in raw_mentions if m]

    try:
        view_count = int(raw.get("viewCount") or 0)
    except (ValueError, TypeError):
        view_count = 0

    return {
        "id": str(raw.get("id", "")),
        "text": raw.get("text", ""),
        "created_at": raw.get("createdAt", ""),
        "url": tweet_url,
        "author": {
            "name": author.get("name", ""),
            "screen_name": screen_name,
            "profile_url": profile_url,
            "followers": author.get("followers", 0),
            "is_verified": bool(author.get("isVerified") or author.get("isBlueVerified")),
            "bio": author.get("description", ""),
            "location": author.get("location", ""),
        },
        "likes": raw.get("likeCount") or 0,
        "retweets": raw.get("retweetCount") or 0,
        "replies": raw.get("replyCount") or 0,
        "quotes": raw.get("quoteCount") or 0,
        "bookmarks": raw.get("bookmarkCount") or 0,
        "views": view_count,
        "is_reply": bool(raw.get("isReply")),
        "is_quote": bool(raw.get("quoted_tweet") or raw.get("quoted_tweet_results")),
        "lang": raw.get("lang", ""),
        "hashtags": hashtags,
        "mentions": mentions,
    }


def search_tweets(
    query: str,
    max_tweets: int = 20,
    days_back: int = 7,
    include_replies: bool = False,
) -> dict:
    """
    Search Twitter for tweets matching a keyword or query.

    Args:
        query:           Search query. Supports Twitter operators: #hashtag, "exact phrase",
                         from:user, to:user, OR, -exclude. Do NOT add since:/until: manually —
                         days_back handles that.
        max_tweets:      Cap on tweets to return (client-side after fetching).
        days_back:       Date window. Passed server-side as since:/until: via Twitter native syntax.
                         0 = no date filter.
        include_replies: Whether to include reply tweets. Default False — original content only.

    Returns:
        dict with keys: query, days_back, tweets, tweet_count, errors
    """
    errors = []
    api_key = os.environ.get("APIFY_API_TOKEN")
    if not api_key:
        return {
            "query": query,
            "days_back": days_back,
            "tweets": [],
            "tweet_count": 0,
            "errors": ["APIFY_API_TOKEN not set"],
        }

    client = ApifyClient(api_key)

    # Build actor input — date window appended to the query as Twitter's native
    # since:/until: operators (applied server-side by the actor).
    search_query = query
    if days_back > 0:
        since = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")
        until = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        search_query = f"{query} since:{since} until:{until}"
    run_input: dict = {"searchTerms": [search_query], "sort": "Latest"}

    # actor bills a minimum of 20 items per run
    platform_max = max(max_tweets, MIN_ITEMS)
    run_input["maxItems"] = platform_max

    print(f"Searching tweets: '{query}' (last {days_back}d, max {max_tweets})", file=sys.stderr)
    raw_items = []
    try:
        with _suppress_apify_logs():
            run = client.actor(ACTOR_ID).call(
                run_input=run_input,
                max_items=platform_max,
                logger=None,
            )
        raw_items = dataset_items(client, run)
    except Exception as e:
        errors.append(f"Actor run failed: {e}")
        return {
            "query": query,
            "days_back": days_back,
            "tweets": [],
            "tweet_count": 0,
            "errors": errors,
        }

    # Parse and filter
    tweets = []
    seen_ids = set()
    for item in raw_items:
        # Skip non-tweet items (e.g. {"noResults": true}) and empties
        if item.get("noResults") or not item.get("text"):
            continue

        tweet = _parse_tweet(item)

        if tweet["id"] in seen_ids:
            continue
        seen_ids.add(tweet["id"])

        if not include_replies and tweet["is_reply"]:
            continue

        tweets.append(tweet)

    tweets = tweets[:max_tweets]

    return {
        "query": query,
        "days_back": days_back,
        "tweets": tweets,
        "tweet_count": len(tweets),
        "errors": errors,
    }


RATE_LIMIT_DELAY = 5  # small gap between calls; actor rotates guest tokens internally


def search_tweets_batch(
    queries: List[str],
    max_tweets: int = 20,
    days_back: int = 7,
    include_replies: bool = False,
) -> List[dict]:
    """
    Search tweets for multiple queries with a small delay between calls.

    Use this instead of looping over search_tweets() — the brief delay keeps
    back-to-back guest-token requests from tripping rate limits.

    Args:
        queries:         List of search queries.
        max_tweets:      Cap on tweets per query.
        days_back:       Date window per query. 0 = no filter.
        include_replies: Whether to include reply tweets.

    Returns:
        List of result dicts, one per query, in input order.
    """
    results = []
    for i, query in enumerate(queries):
        if i > 0:
            print(f"  Waiting {RATE_LIMIT_DELAY}s before next query...", file=sys.stderr)
            import time
            time.sleep(RATE_LIMIT_DELAY)
        results.append(search_tweets(query, max_tweets, days_back, include_replies))
    return results


if __name__ == "__main__":
    input_file = sys.argv[1] if len(sys.argv) > 1 else "example_input.json"
    with open(input_file) as f:
        inp = json.load(f)

    result = search_tweets(
        query=inp["query"],
        max_tweets=inp.get("max_tweets", 20),
        days_back=inp.get("days_back", 7),
        include_replies=inp.get("include_replies", False),
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
