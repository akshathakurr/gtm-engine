# LinkedIn Post Comment Scraper

Extracts comments from a LinkedIn post. No login required.

**Actor:** `apimaestro/linkedin-post-comments-replies-engagements-scraper-no-cookies`
**Pricing:** $1.2/1,000 comments (~$0.0012/comment)

> ⚠️ `raw_sample.json` is a placeholder — field names are confirmed from actor docs but not live-tested. Run a discovery call when Apify credits restore.

---

## Inputs

| Field | Type | Default | Description |
|---|---|---|---|
| `post_url` | string | required | Full LinkedIn post URL or numeric activity ID |
| `max_comments` | int | 20 | Max top-level comments to return |
| `include_replies` | bool | false | Include reply comments. Default false — top-level only |

## Outputs

List of comments with:
- `text`, `date`, `is_edited`, `is_pinned`, `comment_url`
- `author` — name, headline, profile_url
- `reactions` — total, like, appreciation, empathy, praise, interest
- `reply_count` — number of replies on this comment

---

## Usage

```bash
python3 scraper.py example_input.json
```

## Notes

- The actor bills per returned comment, so the scraper passes `limit=max_comments` (capped at the 100/page max) instead of buying a full 100-comment page to keep 20. It paginates via `page_number` only when replies are excluded and it still needs more top-level comments, and stops at `MAX_PAGES` (5) so a reply-heavy post can't run up unbounded spend.
- Top-level comments only by default — set `include_replies=true` to include nested replies.
- Post URL formats accepted: full URL (`https://www.linkedin.com/posts/..._activity-ID-...`) or bare numeric ID.
