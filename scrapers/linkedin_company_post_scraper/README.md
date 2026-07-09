# LinkedIn Company Post Scraper

Fetches recent posts from a LinkedIn company page.

## Actor

`harvestapi/linkedin-company-posts` — $2 per 1,000 posts, no cookies (swapped from `apimaestro/linkedin-company-posts` 2026-07-09; the apimaestro actor is the known-good fallback). Field mappings verified against a live discovery call — `raw_sample.json` holds real actor output. Re-run discovery any time with:

```bash
APIFY_API_TOKEN=... python3 scraper.py --discover https://www.linkedin.com/company/google/
```

Note: some company slugs (e.g. `anthropic`) return 0 posts on every vendor — a LinkedIn-side quirk, not an input error.

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
