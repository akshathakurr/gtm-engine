# LinkedIn Profile Post Scraper

Fetches posts and activity from a LinkedIn user's profile, ordered newest to oldest.

## What it returns
- Full post text (never truncated)
- Post type: original post, quote (repost with comment), or repost
- For quotes/reposts: the original post content and author are included under `reshared_post`
- Engagement stats: reactions, likes, comments, reposts
- Attached media: images, video (with thumbnail), etc.
- Unique `urn` per post for deduplication across runs

## Apify Actor
**ID:** `harvestapi/linkedin-profile-posts`
**Cost:** ~$2 per 1,000 posts
**Login required:** No — works on public profiles without LinkedIn cookies
**Posts per API call:** 100

## Setup

Install the Apify client:
```bash
pip install apify-client
```

Set your API token:
```bash
export APIFY_API_TOKEN=your_token_here
```

## Usage

**From another workflow (import):**
```python
from Scrapers.LinkedIn_Profile_Post_Scraper.scraper import scrape_linkedin_profile_posts

# Default: last 30 posts
result = scrape_linkedin_profile_posts(
    profile_url="https://www.linkedin.com/in/satyanadella/"
)

# Last 60 days of posts
result = scrape_linkedin_profile_posts(
    profile_url="https://www.linkedin.com/in/satyanadella/",
    days_back=60
)

# Posts since a specific date
result = scrape_linkedin_profile_posts(
    profile_url="https://www.linkedin.com/in/satyanadella/",
    since_date="2026-01-01"
)

# Specific count
result = scrape_linkedin_profile_posts(
    profile_url="https://www.linkedin.com/in/satyanadella/",
    max_posts=10
)
```

**From the command line:**
```bash
# Uses example_input.json by default
python scraper.py

# Or pass a custom input file
python scraper.py my_input.json
```

## Input
See `input_schema.json` for full schema.

| Field | Type | Default | Description |
|---|---|---|---|
| `profile_url` | string | required | LinkedIn profile URL |
| `max_posts` | integer | 10 | Max posts to return. Always applies as a hard cap. |
| `days_back` | integer | 60 | Return posts from last N days. Pass `null` to remove date limit. |
| `since_date` | string | — | Return posts on/after this date (YYYY-MM-DD). Used only when `days_back` is null. |

**Default behaviour:** last 60 days, capped at 10 posts. Both limits apply together — you get at most 10 posts from within the 2-month window. A prolific poster with 300 posts in 2 months still only returns 10. Override either limit explicitly when a workflow needs more.

## Output
See `output_schema.json` for full schema and `example_output.json` for a sample.

Key output fields per post:
- `urn` — unique ID, use this for deduplication across runs
- `post_type` — `regular`, `quote`, or `repost`
- `reshared_post` — present on `quote`/`repost` posts; contains the original post
- `timestamp_ms` — epoch ms, use for precise date comparisons

## Notes
- Only scrapes publicly visible posts
- Posts are returned newest → oldest
- The actor caps results server-side via `maxPosts` (and an optional date cutoff), so there's no pagination loop
- To avoid duplicate posts across runs, track seen `urn` values
