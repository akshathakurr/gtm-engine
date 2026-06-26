"""
Twitter Profile Scraper — extract profile info and recent tweets from a Twitter/X account.

Actor: kaitoeasyapi/twitter-x-data-tweet-scraper-pay-per-result-cheapest
  (~$0.18-0.25 per 1,000 tweets). No Twitter/X login or cookies required — uses
  public guest tokens, so it's safe to ship in an OSS tool. Works on the Apify
  free plan. Replaced altimis/scweet, which demanded full X account access.

Input: profile_url, max_tweets (default 50), days_back (default 90), include_retweets (default False)
Output: profile dict + list of parsed tweets

A profile's tweets are fetched via a `from:<handle>` search; the profile object
is extracted from the author block carried on each returned tweet.
Same actor and field structure as the Twitter Research Scraper.
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
TWITTER_DATE_FORMAT = "%a %b %d %H:%M:%S +0000 %Y"
MIN_ITEMS = 20  # actor's minimum billable items per run


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


def _parse_twitter_date(date_str: str) -> Optional[datetime]:
    try:
        return datetime.strptime(date_str, TWITTER_DATE_FORMAT).replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _handle_from_url(url: str) -> str:
    """Extract a bare @handle from a twitter.com / x.com profile URL."""
    import re
    m = re.search(r"(?:twitter|x)\.com/(?:#!/)?@?([A-Za-z0-9_]+)", url or "")
    return m.group(1) if m else ""


def _parse_profile(author: dict) -> dict:
    screen_name = author.get("userName", "")
    # Resolve website URL from the profile's entities if present
    website = ""
    url_entities = ((author.get("entities") or {}).get("url") or {}).get("urls") or []
    for u in url_entities:
        if u.get("expanded_url"):
            website = u["expanded_url"]
            break

    return {
        "name": author.get("name", ""),
        "screen_name": screen_name,
        "profile_url": author.get("twitterUrl") or (f"https://twitter.com/{screen_name}" if screen_name else ""),
        "bio": author.get("description", ""),
        "location": author.get("location", ""),
        "website": website,
        "followers_count": author.get("followers", 0),
        "following_count": author.get("following", 0),
        "tweet_count": author.get("statusesCount", 0),
        "is_verified": bool(author.get("isVerified") or author.get("isBlueVerified")),
        "account_created_at": author.get("createdAt", ""),
        "profile_image_url": author.get("profilePicture", ""),
    }


def _parse_tweet(raw: dict) -> dict:
    """Parse a raw kaitoeasyapi tweet item. Same structure as Twitter Research Scraper."""
    tweet_url = (raw.get("twitterUrl") or raw.get("url") or "").replace("x.com", "twitter.com")

    entities = raw.get("entities") or {}
    raw_hashtags = entities.get("hashtags") or []
    raw_mentions = entities.get("user_mentions") or []
    hashtags = [h.get("text", h) if isinstance(h, dict) else h for h in raw_hashtags if h]
    mentions = [m.get("screen_name", m) if isinstance(m, dict) else m for m in raw_mentions if m]

    try:
        view_count = int(raw.get("viewCount") or 0)
    except (ValueError, TypeError):
        view_count = 0

    # Detect retweets: retweeted_tweet present or text starts with "RT @"
    text = raw.get("text") or ""
    is_retweet = bool(raw.get("retweeted_tweet")) or text.startswith("RT @")

    return {
        "id": str(raw.get("id", "")),
        "text": text,
        "created_at": raw.get("createdAt", ""),
        "url": tweet_url,
        "likes": raw.get("likeCount") or 0,
        "retweets": raw.get("retweetCount") or 0,
        "replies": raw.get("replyCount") or 0,
        "quotes": raw.get("quoteCount") or 0,
        "bookmarks": raw.get("bookmarkCount") or 0,
        "views": view_count,
        "is_retweet": is_retweet,
        "is_reply": bool(raw.get("isReply")),
        "is_quote": bool(raw.get("quoted_tweet") or raw.get("quoted_tweet_results")),
        "lang": raw.get("lang", ""),
        "hashtags": hashtags,
        "mentions": mentions,
    }


def scrape_twitter_profile(
    profile_url: str,
    max_tweets: int = 50,
    days_back: int = 90,
    include_retweets: bool = False,
) -> dict:
    """
    Scrape a Twitter/X profile and return profile info + recent tweets.

    Args:
        profile_url:      Full Twitter/X profile URL (e.g. https://twitter.com/paulg)
        max_tweets:       Cap on tweets returned (client-side).
        days_back:        Only include tweets from the last N days. 0 = no filter.
        include_retweets: Whether to include retweets. Default False — original content only.

    Returns:
        dict with keys: profile_url, profile, tweets, tweet_count, date_range, errors
    """
    errors = []
    api_key = os.environ.get("APIFY_API_TOKEN")
    if not api_key:
        return {
            "profile_url": profile_url,
            "profile": None,
            "tweets": [],
            "tweet_count": 0,
            "date_range": {},
            "errors": ["APIFY_API_TOKEN not set"],
        }

    client = ApifyClient(api_key)

    # Fetch a profile's tweets via a from:<handle> search; date window appended
    # as Twitter's native since:/until: operators.
    handle = _handle_from_url(profile_url)
    if not handle:
        return {
            "profile_url": profile_url,
            "profile": None,
            "tweets": [],
            "tweet_count": 0,
            "date_range": {},
            "errors": [f"Could not extract handle from URL: {profile_url}"],
        }

    search_query = f"from:{handle}"
    if days_back > 0:
        since = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")
        until = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        search_query = f"{search_query} since:{since} until:{until}"
    run_input = {"searchTerms": [search_query], "sort": "Latest"}

    # actor bills a minimum of 20 items per run
    platform_max = max(max_tweets, MIN_ITEMS)
    run_input["maxItems"] = platform_max

    print(f"Fetching tweets for: {profile_url}", file=sys.stderr)
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
            "profile_url": profile_url,
            "profile": None,
            "tweets": [],
            "tweet_count": 0,
            "date_range": {},
            "errors": errors,
        }

    if not raw_items:
        return {
            "profile_url": profile_url,
            "profile": None,
            "tweets": [],
            "tweet_count": 0,
            "date_range": {},
            "errors": ["Actor returned 0 items"],
        }

    # Extract profile from the first tweet's author block
    profile = None
    for item in raw_items:
        author = item.get("author")
        if author and author.get("userName"):
            profile = _parse_profile(author)
            break

    # Parse and filter tweets
    cutoff = None
    if days_back > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)

    tweets = []
    seen_ids = set()
    for item in raw_items:
        if item.get("noResults") or not item.get("text"):
            continue

        tweet = _parse_tweet(item)

        if tweet["id"] in seen_ids:
            continue
        seen_ids.add(tweet["id"])

        if not include_retweets and tweet["is_retweet"]:
            continue

        if cutoff and tweet["created_at"]:
            tweet_dt = _parse_twitter_date(tweet["created_at"])
            if tweet_dt and tweet_dt < cutoff:
                continue

        tweets.append(tweet)

    tweets = tweets[:max_tweets]

    date_range = {}
    if tweets:
        date_range = {
            "newest": tweets[0]["created_at"],
            "oldest": tweets[-1]["created_at"],
        }

    return {
        "profile_url": profile_url,
        "profile": profile,
        "tweets": tweets,
        "tweet_count": len(tweets),
        "date_range": date_range,
        "errors": errors,
    }


RATE_LIMIT_DELAY = 5  # small gap between calls; actor rotates guest tokens internally


def scrape_twitter_profiles_batch(
    profile_urls: List[str],
    max_tweets: int = 50,
    days_back: int = 90,
    include_retweets: bool = False,
) -> List[dict]:
    """
    Scrape multiple Twitter profiles with a small delay between calls.

    Use this instead of looping over scrape_twitter_profile() — the brief delay
    keeps back-to-back guest-token requests from tripping rate limits.

    Args:
        profile_urls:     List of Twitter/X profile URLs.
        max_tweets:       Cap on tweets per profile.
        days_back:        Date window per profile. 0 = no filter.
        include_retweets: Whether to include retweets.

    Returns:
        List of result dicts, one per profile, in input order.
    """
    results = []
    for i, url in enumerate(profile_urls):
        if i > 0:
            print(f"  Waiting {RATE_LIMIT_DELAY}s before next profile...", file=sys.stderr)
            import time
            time.sleep(RATE_LIMIT_DELAY)
        results.append(scrape_twitter_profile(url, max_tweets, days_back, include_retweets))
    return results


if __name__ == "__main__":
    input_file = sys.argv[1] if len(sys.argv) > 1 else "example_input.json"
    with open(input_file) as f:
        inp = json.load(f)

    result = scrape_twitter_profile(
        profile_url=inp["profile_url"],
        max_tweets=inp.get("max_tweets", 50),
        days_back=inp.get("days_back", 90),
        include_retweets=inp.get("include_retweets", False),
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
