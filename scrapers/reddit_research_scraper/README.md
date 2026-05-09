# Reddit Research Scraper

Search Reddit posts by keyword, optionally scoped to a subreddit.

**Free — no Apify, no API key required.** Uses Reddit's public JSON API.

---

## Inputs

| Field | Type | Default | Description |
|---|---|---|---|
| `query` | string | required | Search query. Supports Reddit operators: `site:`, `author:`, `OR`, `-exclude` |
| `subreddit` | string \| null | null | Scope to a subreddit e.g. `"SaaS"`, `"entrepreneur"`. Null = all Reddit |
| `sort` | string | `"new"` | `"new"`, `"top"`, `"relevance"`, `"hot"`, `"comments"` |
| `time_filter` | string | `"week"` | `"hour"`, `"day"`, `"week"`, `"month"`, `"year"`, `"all"` |
| `max_posts` | int | 25 | Max posts to return |

## Outputs

List of posts with:
- `title`, `text`, `url`, `external_url`
- `author`, `subreddit`, `flair`
- `score`, `upvote_ratio`, `num_comments`
- `created_at`, `is_self`

---

## Usage

```bash
python3 scraper.py example_input.json
```

## Batch

```python
from scraper import search_reddit_batch
results = search_reddit_batch(
    queries=["notion pain points", "notion alternatives", "notion vs obsidian"],
    subreddit="productivity",
    sort="top",
    time_filter="month"
)
```

## Notes

- No rate limit concerns for GTM-scale usage (60 req/min unauthenticated).
- `sort="top"` with `time_filter="month"` is best for finding high-signal posts.
- `sort="new"` is best for real-time monitoring.
- Text posts have `text` populated; link posts have `external_url` instead.
