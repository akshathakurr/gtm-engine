# Twitter Research Scraper

Search and scrape Twitter/X posts by keyword, topic, or brand. Use for brand mentions, competitor monitoring, and discussion analysis.

**Actor:** `kaitoeasyapi/twitter-x-data-tweet-scraper-pay-per-result-cheapest` — ~$0.18-0.25 / 1,000 tweets (20-item minimum per run). **No Twitter/X login or cookies required** (public guest tokens), works on the Apify free plan. Replaced `altimis/scweet`, which demanded full X account access.

## What it does

1. Runs a keyword search against Twitter via the Apify actor
2. Applies date filtering server-side using Twitter's native `since:`/`until:` syntax — reliable, no over-fetching
3. Filters out reply tweets (optional)
4. Returns parsed tweets with full author info and engagement metrics

## Inputs

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `query` | string | Yes | — | Search query. Supports Twitter operators (see below) |
| `max_tweets` | integer | No | 20 | Cap on tweets returned (client-side) |
| `days_back` | integer | No | 7 | Date window. Applied server-side via `since:`/`until:`. 0 = no filter |
| `include_replies` | boolean | No | false | Whether to include reply tweets |

### Query syntax (Twitter native operators)

| Operator | Example | Effect |
|---|---|---|
| Plain keywords | `cold email outbound` | Tweets containing all words |
| Exact phrase | `"cold email"` | Exact phrase match |
| OR | `cold email OR outbound` | Either term |
| Exclude | `cold email -spam` | Exclude a word |
| Hashtag | `#outbound` | By hashtag |
| From user | `from:user` | Tweets by a specific account |
| Mention | `@company` | Tweets mentioning an account |

**Do NOT add `since:`/`until:` manually** — use `days_back` instead. The actor handles it.

## Outputs

| Field | Type | Description |
|---|---|---|
| `query` | string | Search query used |
| `days_back` | integer | Date window applied |
| `tweets` | array | Parsed tweets |
| `tweet_count` | integer | Number of tweets returned |
| `errors` | array | Any errors |

### Tweet object fields

| Field | Type | Description |
|---|---|---|
| `id` | string | Tweet ID |
| `text` | string | Full tweet text |
| `created_at` | string | Raw Twitter date string |
| `url` | string | Direct tweet link (twitter.com) |
| `author.name` | string | Display name |
| `author.screen_name` | string | @handle |
| `author.profile_url` | string | Twitter profile link |
| `author.followers` | integer | Follower count |
| `author.is_verified` | boolean | Blue check status |
| `author.bio` | string | Profile bio |
| `author.location` | string | Profile location |
| `likes` | integer | Favorite count |
| `retweets` | integer | Retweet count |
| `replies` | integer | Reply count |
| `quotes` | integer | Quote tweet count |
| `bookmarks` | integer | Bookmark count |
| `views` | integer | View count |
| `is_reply` | boolean | Whether this is a reply |
| `is_quote` | boolean | Whether this quotes another tweet |
| `lang` | string | Detected language |
| `hashtags` | array | Hashtags used |
| `mentions` | array | @mentions used |

## Usage

```bash
python3 scraper.py                  # uses example_input.json
python3 scraper.py my_input.json    # custom input
```

## Dependencies

```bash
pip install apify-client python-dotenv
```

## Pricing

- ~$0.18-0.25 per 1,000 tweets (pay-per-result)
- 20-item minimum billed per run
- Typical 20-tweet run: ~$0.005

## Notes

- Reply tweets are excluded by default. Set `include_replies: true` to include them.
- Date filtering is server-side (Twitter native `since:`/`until:`) — only tweets in the window are fetched.
- The actor fetches in batches; `max_tweets` caps the output client-side.
- For sentiment/intent analysis, pass `tweets[].text` to an LLM — this scraper returns raw data only.
