# Website Scraper

Extracts structured company information from a website. No external APIs — uses `requests` + `BeautifulSoup4` (standard Python libs).

## What it does

1. Fetches the homepage
2. Discovers key subpages from the nav (about, product, pricing, careers, customers)
3. Fetches up to 6 subpages in parallel
4. Extracts structured fields from combined page text

## Inputs

| Field | Type | Required | Description |
|---|---|---|---|
| `url` | string | Yes | Company homepage URL (e.g. `https://acko.com`) |

## Outputs

| Field | Type | Description |
|---|---|---|
| `url` | string | Normalized homepage URL |
| `company_name` | string | From og:title or `<title>` |
| `meta_description` | string | From meta description or og:description |
| `homepage_text` | string | Cleaned homepage text (max 3000 chars) |
| `pages_scraped` | list | Labels of pages that were scraped |
| `product_description` | string | Best available product/company description |
| `customers` | list | Customer names found in page copy |
| `employee_size_hint` | string | Raw headcount number if found |
| `founded_year` | string | Year founded if found |
| `icp_hints` | list | Target customer hints from copy |
| `full_text_by_page` | object | Page label → extracted text (max 3000 chars each) |
| `errors` | list | Any pages that failed to fetch |

## Usage

```bash
python3 scraper.py                 # uses example_input.json
python3 scraper.py my_input.json   # custom input
```

## Dependencies

```bash
pip install requests beautifulsoup4
```

## Notes

- Works on static/SSR sites. JS-heavy SPAs may return thin text — the `full_text_by_page` will show this clearly.
- Key page discovery is heuristic (looks for patterns like `/about`, `/pricing`, `/careers` in nav links).
- `full_text_by_page` contains the raw text per page — this is the richest output and should be passed to workflows for LLM analysis.
- Respects a 15s timeout per page; failed pages are listed in `errors`, not raised as exceptions.
