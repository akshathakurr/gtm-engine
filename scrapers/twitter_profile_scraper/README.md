# Twitter Profile Scraper

Extract profile info and recent tweets from a Twitter/X account.

**Actor:** `altimis/scweet` — $0.003/tweet + $0.01 run-start (free plan). ~$0.07-0.10 per run.
Same actor as the Twitter Research Scraper — one actor, consistent field structure.

## What it does

1. Takes a Twitter/X profile URL
2. Fetches recent tweets via `altimis/scweet` with `profile_urls` input
3. Filters retweets (optional) and applies `days_back` date cap client-side
4. Returns profile metadata + parsed tweet list

## Inputs

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `profile_url` | string | Yes | — | Full Twitter/X profile URL (e.g. `https://twitter.com/paulg`) |
| `max_tweets` | integer | No | 50 | Cap on tweets returned |
| `days_back` | integer | No | 90 | Only include tweets from the last N days. 0 = no filter |
| `include_retweets` | boolean | No | false | Whether to include retweets |

## Outputs

| Field | Type | Description |
|---|---|---|
| `profile_url` | string | Input URL |
| `profile` | object | Profile metadata (see below) |
| `tweets` | array | Parsed tweets (see below) |
| `tweet_count` | integer | Number of tweets returned |
| `date_range` | object | `oldest` and `newest` tweet timestamps |
| `errors` | array | Any errors |

### Profile fields

| Field | Type | Description |
|---|---|---|
| `name` | string | Display name |
| `screen_name` | string | @handle |
| `profile_url` | string | Twitter profile link |
| `bio` | string | Profile bio |
| `location` | string | Profile location |
| `website` | string | Expanded website URL from profile |
| `followers_count` | integer | Follower count |
| `following_count` | integer | Following count |
| `tweet_count` | integer | Total tweets ever posted |
| `is_verified` | boolean | Blue check status |
| `account_created_at` | string | Account creation date |
| `profile_image_url` | string | Profile picture URL |

### Tweet fields

| Field | Type | Description |
|---|---|---|
| `id` | string | Tweet ID |
| `text` | string | Full tweet text |
| `created_at` | string | Raw Twitter date string |
| `url` | string | Direct tweet link |
| `likes` | integer | Favorite count |
| `retweets` | integer | Retweet count |
| `replies` | integer | Reply count |
| `quotes` | integer | Quote tweet count |
| `bookmarks` | integer | Bookmark count |
| `views` | integer | View count |
| `is_retweet` | boolean | Whether this is a retweet |
| `is_reply` | boolean | Whether this is a reply |
| `is_quote` | boolean | Whether this quotes another tweet |
| `lang` | string | Detected language |
| `hashtags` | array | Hashtags used |
| `mentions` | array | @mentions used |

## Usage

```bash
python3 scraper.py                 # uses example_input.json
python3 scraper.py my_input.json   # custom input
```

## Dependencies

```bash
pip install apify-client python-dotenv
```

## Notes

- Retweets excluded by default (`include_retweets: false`).
- The actor fetches in batches. `max_tweets` caps output client-side.
- Switched from `quacker/twitter-scraper` (4GB browser crawler, $0.35/run) to `altimis/scweet` — 5x cheaper, 2x more tweets, richer fields (views, bookmarks, is_reply).
