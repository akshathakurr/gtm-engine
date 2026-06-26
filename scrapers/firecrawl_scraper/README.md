# Firecrawl Scraper

Fetches web pages as clean **markdown**, with JavaScript rendering and
anti-bot/proxy handling done by [Firecrawl](https://firecrawl.dev).

## Why this exists

The static `website_scraper` (requests + BeautifulSoup) returns the *shell* of a
JS-heavy page — nav, footer, boilerplate — but misses content that loads via
JavaScript. On modern SaaS sites that means **pricing tables**, **case-study /
blog indexes**, feature sections, and more come back thin or empty. Firecrawl
renders the page first, so that content becomes reliable, readable markdown.

## Two entry points

| Function | Returns | Use |
|----------|---------|-----|
| `scrape_markdown(url)` | `str \| None` | One page as markdown |
| `scrape_website(url)` | `dict \| None` | Homepage + key subpages, in the **same shape** as `website_scraper.scrape_website()` — a drop-in replacement |

`scrape_website()` fetches the homepage (markdown + html), discovers key
subpages (about / product / pricing / customers) by reusing
`website_scraper`'s link discovery, and scrapes them via Firecrawl (2 at a
time, the free-tier limit). If no pricing page is discovered it guesses
`{homepage}/pricing`. Text-parsing heuristics (customers, founded year,
headcount, ICP hints, logos) are reused from `website_scraper` — only the
*fetching* changed.

Both functions return `None` when:
- `FIRECRAWL_API_KEY` is unset (graceful no-op — callers fall back to the static
  scraper), or
- the request fails / returns nothing.

## Inputs (CLI / example_input.json)

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `url` | string | — | Page (markdown mode) or homepage (website mode) |
| `mode` | string | — | `"website"` for `scrape_website`; omit for single-page markdown |
| `wait_ms` | int | 3000 | Wait for JS to render before capture |
| `only_main_content` | bool | true | Strip nav/header/footer chrome |

## API & cost

- REST: `POST https://api.firecrawl.dev/v2/scrape`, `Authorization: Bearer <key>`.
- Free plan: **1,000 credits/month**, **1 credit/page**, 2 concurrent, low rate
  limit. `scrape_website` fetches homepage + up to 5 subpages ≈ **6 credits/company**,
  so 1,000 credits ≈ 150+ companies/month — far more than any competitor run needs.
- Needs `FIRECRAWL_API_KEY` in `.env`. No SDK dependency — uses `requests`.

## Used by

- `workflows/competitor_analysis` — **primary** website scraper when a key is
  set (falls back to the static `website_scraper` when it isn't). Lifts data
  quality across all website-derived fields: description, persona, CTA, pricing,
  customer stories, features, messaging, SEO.

## Run standalone

```bash
# single page as markdown
.venv/bin/python scrapers/firecrawl_scraper/scraper.py scrapers/firecrawl_scraper/example_input.json

# full website (homepage + subpages) — set "mode": "website" in the input
```
