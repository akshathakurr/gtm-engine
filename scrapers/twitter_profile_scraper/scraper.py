"""
Twitter Profile Scraper — extract profile info and recent tweets from a Twitter/X account.

Actor: altimis/scweet (PAY_PER_EVENT — $0.003/tweet + $0.01 run-start on free plan)
  ~$0.07-0.10 per run for ~100 tweets.

Input: profile_url, max_tweets (default 50), days_back (default 90), include_retweets (default False)
Output: profile dict + list of parsed tweets

Same actor and field structure as the Twitter Research Scraper (altimis/scweet).
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
TWITTER_DATE_FORMAT = "%a %b %d %H:%M:%S +0000 %Y"


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


def _parse_twitter_date(date_str: str) -> Optional[datetime]:
    try:
        return datetime.strptime(date_str, TWITTER_DATE_FORMAT).replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _parse_profile(user: dict) -> dict:
    screen_name = user.get("handle") or user.get("screen_name", "")
    # Resolve website URL from expanded urls list if available
    website = ""
    for u in user.get("urls", []):
        if u.get("expanded_url"):
            website = u["expanded_url"]
            break
    if not website:
        website = user.get("url", "")

    return {
        "name": user.get("name", ""),
        "screen_name": screen_name,
        "profile_url": f"https://twitter.com/{screen_name}" if screen_name else "",
        "bio": user.get("description", ""),
        "location": user.get("location", ""),
        "website": website,
        "followers_count": user.get("followers_count", 0),
        "following_count": user.get("friends_count", 0),
        "tweet_count": user.get("statuses_count", 0),
        "is_verified": bool(user.get("is_blue_verified") or user.get("verified")),
        "account_created_at": user.get("created_at", ""),
        "profile_image_url": user.get("profile_image_url_https", ""),
    }


def _parse_tweet(raw: dict) -> dict:
    """Parse a raw altimis/scweet item. Same structure as Twitter Research Scraper."""
    user = raw.get("user") or {}
    tweet = raw.get("tweet") or {}
    screen_name = raw.get("handle") or user.get("handle", "")

    tweet_url = (raw.get("tweet_url") or tweet.get("tweet_url", "")).replace("x.com", "twitter.com")

    entities = tweet.get("entities") or {}
    raw_hashtags = entities.get("hashtags") or tweet.get("hashtags") or []
    raw_mentions = entities.get("mentions") or tweet.get("mentions") or []
    hashtags = [h.get("text", h) if isinstance(h, dict) else h for h in raw_hashtags if h]
    mentions = [m.get("screen_name", m) if isinstance(m, dict) else m for m in raw_mentions if m]

    try:
        view_count = int(raw.get("view_count") or tweet.get("view_count") or 0)
    except (ValueError, TypeError):
        view_count = 0

    # Detect retweets: text starts with "RT @" or retweeted_status exists
    text = raw.get("text") or tweet.get("text", "")
    is_retweet = text.startswith("RT @")

    return {
        "id": str(raw.get("id", "")).replace("tweet-", ""),
        "text": text,
        "created_at": raw.get("created_at", ""),
        "url": tweet_url,
        "likes": raw.get("favorite_count") or tweet.get("favorite_count") or 0,
        "retweets": raw.get("retweet_count") or tweet.get("retweet_count") or 0,
        "replies": raw.get("reply_count") or tweet.get("reply_count") or 0,
        "quotes": raw.get("quote_count") or tweet.get("quote_count") or 0,
        "bookmarks": raw.get("bookmark_count") or tweet.get("bookmark_count") or 0,
        "views": view_count,
        "is_retweet": is_retweet,
        "is_reply": bool(raw.get("is_reply")),
        "is_quote": bool(raw.get("is_quote")),
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

    # Normalize to x.com — actor returns source_value as x.com URLs
    normalized_url = profile_url.replace("twitter.com", "x.com")
    run_input = {"profile_urls": [normalized_url]}

    # Pass days_back as since/until if needed
    if days_back > 0:
        run_input["since"] = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")
        run_input["until"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # min 13 items to exceed the $0.036 minimum charge (13 * $0.003 = $0.039)
    platform_max = max(max_tweets, 13)

    print(f"Fetching tweets for: {profile_url}", file=sys.stderr)
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

    # Extract profile from first tweet's user object
    profile = None
    for item in raw_items:
        user = item.get("user")
        if user and user.get("handle"):
            profile = _parse_profile(user)
            break

    # Parse and filter tweets
    cutoff = None
    if days_back > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)

    tweets = []
    seen_ids = set()
    for item in raw_items:
        if not (item.get("text") or (item.get("tweet") or {}).get("text")):
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


RATE_LIMIT_DELAY = 30  # seconds between calls — avoids Twitter session throttling


def scrape_twitter_profiles_batch(
    profile_urls: List[str],
    max_tweets: int = 50,
    days_back: int = 90,
    include_retweets: bool = False,
) -> List[dict]:
    """
    Scrape multiple Twitter profiles with rate-limit-safe delays between calls.

    Always use this instead of looping over scrape_twitter_profile() — the 30s
    delay between calls prevents Twitter from throttling the actor's session.

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
