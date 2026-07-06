# Reddit Research Scraper

Search Reddit posts by keyword, optionally scoped to a subreddit.

**Free first, paid fallback.** Uses Reddit's public JSON API (free, no key). Reddit
IP-blocks datacenter/cloud ranges (HTTP 403), so when the free API fails the scraper
automatically falls back to the Apify `trudax/reddit-scraper-lite` actor
(residential proxy, ~$0.04/search, needs `APIFY_API_TOKEN`). In fallback mode
`score` / `upvote_ratio` / `num_comments` come back as `0` — the lite actor can't
return engagement counts (its `includeMediaLinks` option is broken and returns 0
items; verified 2026-07-06).

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
