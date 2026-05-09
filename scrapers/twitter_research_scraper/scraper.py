"""
Twitter Research Scraper — search tweets by keyword, topic, or brand.

Actor: altimis/scweet (PAY_PER_EVENT — $0.003/tweet + $0.01 run-start on free plan)
  ~$0.06-0.10 per run for a typical keyword search.

Input: search_query, max_tweets (default 20), days_back (default 7), include_replies (default False)
Output: list of tweets with author info, text, engagement metrics, tweet URL

Date filtering: passed as `since`/`until` (YYYY-MM-DD) to the actor — applied server-side
via Twitter's native since:/until: syntax. Reliable and no over-fetching.
"""

import os
import sys
import json
from datetime import datetime, timezone, timedelta
from typing import Optional, List
from contextlib import contextmanager

from dotenv import load_dotenv
from apify_client import ApifyClient

load_dotenv(os.path.join(os.path.dirname(__file__), "../../.env"))

ACTOR_ID = "altimis/scweet"
TWITTER_DATE_FORMAT = "%a %b %d %H:%M:%S +0000 %Y"  # "Sun Apr 05 15:36:19 +0000 2026"
MIN_RUN_CHARGE_USD = 0.036  # Apify minimum charge per run for this actor
COST_PER_TWEET_USD = 0.003  # Free plan pricing


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


def _parse_tweet(raw: dict) -> dict:
    """Parse a raw altimis/scweet item into clean output fields."""
    user = raw.get("user") or {}
    tweet = raw.get("tweet") or {}
    screen_name = raw.get("handle") or user.get("handle", "")

    # tweet_url uses x.com — normalize to twitter.com for consistency
    tweet_url = (raw.get("tweet_url") or tweet.get("tweet_url", "")).replace("x.com", "twitter.com")
    profile_url = f"https://twitter.com/{screen_name}" if screen_name else ""

    # Hashtags and mentions live in tweet.entities
    entities = tweet.get("entities") or {}
    # Hashtags/mentions can be list of dicts or list of strings depending on the tweet
    raw_hashtags = entities.get("hashtags") or tweet.get("hashtags") or []
    raw_mentions = entities.get("mentions") or tweet.get("mentions") or []
    hashtags = [h.get("text", h) if isinstance(h, dict) else h for h in raw_hashtags if h]
    mentions = [m.get("screen_name", m) if isinstance(m, dict) else m for m in raw_mentions if m]

    # view_count comes as a string
    try:
        view_count = int(raw.get("view_count") or tweet.get("view_count") or 0)
    except (ValueError, TypeError):
        view_count = 0

    return {
        "id": str(raw.get("id", "")).replace("tweet-", ""),
        "text": raw.get("text") or tweet.get("text", ""),
        "created_at": raw.get("created_at", ""),
        "url": tweet_url,
        "author": {
            "name": user.get("name", ""),
            "screen_name": screen_name,
            "profile_url": profile_url,
            "followers": user.get("followers_count", 0),
            "is_verified": bool(user.get("is_blue_verified") or user.get("verified")),
            "bio": user.get("description", ""),
            "location": user.get("location", ""),
        },
        "likes": raw.get("favorite_count") or tweet.get("favorite_count") or 0,
        "retweets": raw.get("retweet_count") or tweet.get("retweet_count") or 0,
        "replies": raw.get("reply_count") or tweet.get("reply_count") or 0,
        "quotes": raw.get("quote_count") or tweet.get("quote_count") or 0,
        "bookmarks": raw.get("bookmark_count") or tweet.get("bookmark_count") or 0,
        "views": view_count,
        "is_reply": bool(raw.get("is_reply")),
        "is_quote": bool(raw.get("is_quote")),
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

    # Build actor input — date filtering passed as since/until (actor appends to query as since:/until:)
    run_input: dict = {"search_query": query}
    if days_back > 0:
        run_input["since"] = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")
        run_input["until"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # min 13 items to exceed the $0.036 minimum charge (13 * $0.003 = $0.039)
    platform_max = max(max_tweets, 13)

    print(f"Searching tweets: '{query}' (last {days_back}d, max {max_tweets})", file=sys.stderr)
    raw_items = []
    try:
        with _suppress_apify_logs():
            run = client.actor(ACTOR_ID).call(
                run_input=run_input,
                max_items=platform_max,
            )
        raw_items = list(client.dataset(run["defaultDatasetId"]).iterate_items())
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
        # Skip non-tweet items
        if not (item.get("text") or (item.get("tweet") or {}).get("text")):
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


RATE_LIMIT_DELAY = 30  # seconds between calls — avoids Twitter session throttling


def search_tweets_batch(
    queries: List[str],
    max_tweets: int = 20,
    days_back: int = 7,
    include_replies: bool = False,
) -> List[dict]:
    """
    Search tweets for multiple queries with rate-limit-safe delays between calls.

    Always use this instead of looping over search_tweets() — the 30s delay between
    calls prevents Twitter from throttling the actor's session.

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
