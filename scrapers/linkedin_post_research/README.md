# LinkedIn Post Research Scraper

Search LinkedIn posts by keyword. Useful for intent signals, competitor monitoring, and content research.

**No LinkedIn account or cookies required.**

---

## Actor

`harvestapi/linkedin-post-search`

**Cost:** $0.002/post on Apify free tier. For 20 posts = ~$0.10/search.

---

## Inputs

| Field | Type | Default | Description |
|---|---|---|---|
| `keyword` | string | required | Search term. Supports hashtags (`#saas`), phrases, keywords. |
| `sort` | string | `"date_posted"` | `"date_posted"` (newest first) or `"relevance"` |
| `max_posts` | int | 20 | Max posts to return |
| `date_filter` | string \| null | null | Optional date filter (undocumented by actor — leave null) |

## Outputs

List of posts with:
- `post_id`, `post_url`, `text`
- `author.name`, `author.headline`, `author.profile_url`
- `stats.reactions`, `stats.comments`, `stats.shares`
- `posted_at` (datetime string), `posted_at_timestamp` (ms)
- `hashtags`, `content_type`, `is_reshare`
- `total_available` — total LinkedIn results for the query (not all fetched)

---

## Usage

```bash
python3 scraper.py example_input.json
```

## Batch

```python
from scraper import search_linkedin_posts_batch
results = search_linkedin_posts_batch(
    keywords=["hiring SDR", "looking for CRM tool", "sales automation"],
    sort="date_posted",
    max_posts=20,
)
```

## Notes

- `sort="date_posted"` gives newest posts first — best for intent signal monitoring.
- `sort="relevance"` gives best match — best for research on a topic.
- The actor paginates in pages of 50. `max_posts` caps client-side.
- Each post costs $0.002. Keep `max_posts` ≤ 20 for day-to-day use.
- `total_available` shows how many posts LinkedIn has for the query (often hundreds).
