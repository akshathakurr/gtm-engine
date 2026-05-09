# Hacker News Scraper

Search HN stories by keyword and/or type. Free, no API key, no Apify.

**API:** Algolia HN Search API (`hn.algolia.com/api/v1`) — completely free and public.

## What it does

- Keyword search across all HN stories (title + content)
- Filter by story type: regular story, Ask HN, Show HN, job, poll
- Sort by date (newest first) or relevance (best match first)
- Server-side date filtering via `days_back` — no over-fetching
- Paginates automatically until `max_results` is reached

## Inputs

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `query` | string | No | `""` | Keyword(s) to search. Empty = fetch recent stories of the given type |
| `story_type` | string | No | `"story"` | One of: `story`, `ask_hn`, `show_hn`, `job`, `poll` |
| `sort_by` | string | No | `"date"` | `"date"` (newest first) or `"relevance"` (best match first) |
| `days_back` | integer | No | `30` | Only include stories from the last N days. `0` = no filter |
| `max_results` | integer | No | `30` | Cap on number of stories returned |

## Outputs

| Field | Type | Description |
|---|---|---|
| `query` | string | Search keyword used |
| `story_type` | string | Tag filter applied |
| `sort_by` | string | Sort order used |
| `days_back` | integer | Date window applied |
| `stories` | array | List of HN stories |
| `story_count` | integer | Number of stories returned |
| `errors` | array | Any fetch errors |

### Story object fields

| Field | Type | Description |
|---|---|---|
| `id` | string | HN story ID |
| `title` | string | Story title |
| `url` | string | External article URL (empty for Ask HN / job posts) |
| `hn_url` | string | Direct HN discussion link |
| `author` | string | HN username of submitter |
| `created_at` | string | ISO 8601 timestamp |
| `points` | integer | Upvote score |
| `num_comments` | integer | Comment count |

## Usage

```bash
python3 scraper.py                  # uses example_input.json
python3 scraper.py my_input.json    # custom input
```

## Dependencies

```bash
pip install requests
```

## Example queries

```json
{"query": "AI sales", "story_type": "story", "sort_by": "relevance", "days_back": 90, "max_results": 20}
{"query": "", "story_type": "ask_hn", "sort_by": "date", "days_back": 7, "max_results": 10}
{"query": "GTM founders", "story_type": "show_hn", "sort_by": "date", "days_back": 30, "max_results": 15}
```

## Notes

- `sort_by: "date"` uses the `search_by_date` endpoint — returns newest first. Best for signal tracking.
- `sort_by: "relevance"` uses the `search` endpoint — returns highest-ranked matches. Best for finding canonical discussions.
- Date filtering is done server-side — only stories within `days_back` are fetched.
- For Ask HN / Show HN posts, `url` is empty — the content lives in the HN thread itself.
