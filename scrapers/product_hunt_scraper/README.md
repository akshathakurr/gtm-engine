# Product Hunt Scraper

Fetches recent product launches from Product Hunt via the public RSS feed.

**No Apify, no API key, no proxy required.**

> **Note:** Product Hunt's Cloudflare protection blocks all datacenter IPs. Individual product pages, topic-specific feeds, and all Apify actors are inaccessible on the free plan. This scraper uses the one endpoint that works: the global RSS feed.

---

## Inputs

| Field | Type | Default | Description |
|---|---|---|---|
| `max_products` | int | 20 | Max products to return. RSS hard cap is 50. |
| `days_back` | int | 1 | Only include launches from the last N days. 0 = no filter. |

## Outputs

List of products with:
- `name` — product name
- `tagline` — one-line description
- `ph_url` — Product Hunt page URL
- `post_id` — numeric PH post ID
- `published_at` — ISO 8601 launch timestamp
- `submitter` — name of the person who submitted it

**Not available** (Cloudflare blocks): upvotes, topics/tags, product website, maker details.

---

## Usage

```bash
python3 scraper.py example_input.json
```
