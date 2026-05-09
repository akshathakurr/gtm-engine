# LinkedIn Company Post Scraper

Fetches recent posts from a LinkedIn company page.

## Actor

`apimaestro/linkedin-company-posts` — same provider as the working LinkedIn Profile Post Scraper.

## ⚠️ Discovery call pending

This scraper was built without a live discovery call because the Apify account hit its monthly limit. Field names in `scraper.py` are based on `apimaestro`'s profile-post conventions and may not match exactly. When credits are restored:

```bash
APIFY_API_TOKEN=... python3 scraper.py --discover https://www.linkedin.com/company/anthropic/
```

This will overwrite `raw_sample.json` with the real actor output. Then read `raw_sample.json` and fix any field mappings in `_parse_post`, `_parse_author`, `_parse_stats` before using the scraper in production.

## Inputs

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `company_url` | string | Yes | — | LinkedIn company page URL (e.g. `https://www.linkedin.com/company/anthropic/`) |
| `max_posts` | integer | No | 10 | Maximum posts to return (1–100) |

## Outputs

```json
{
  "company_url": "https://www.linkedin.com/company/anthropic/",
  "total": 10,
  "posts": [
    {
      "urn": "urn:li:activity:...",
      "url": "https://www.linkedin.com/feed/update/...",
      "post_type": "regular",
      "posted_at": "2026-04-08 14:23:00",
      "timestamp_ms": 1744121780000,
      "text": "Full post text...",
      "author": { "name": "Anthropic", "username": "anthropic", "url": "..." },
      "stats": { "total_reactions": 1840, "likes": 1602, "comments": 187, "reposts": 214 },
      "media": null,
      "reshared_post": null
    }
  ],
  "errors": []
}
```

Posts are always ordered **newest → oldest**.

## Usage

```bash
# Run with default example input
APIFY_API_TOKEN=... python3 scraper.py

# Run with custom input file
APIFY_API_TOKEN=... python3 scraper.py my_input.json

# Discovery mode — dumps raw actor output to raw_sample.json
APIFY_API_TOKEN=... python3 scraper.py --discover https://www.linkedin.com/company/openai/
```

## Notes

- `repost` type items (pure reshares with no added text) are included in the output — filtering by `post_type` is the workflow's responsibility.
- Apify actor logs are suppressed to avoid flooding the terminal.
- `max_posts` caps the total result count regardless of pagination.
