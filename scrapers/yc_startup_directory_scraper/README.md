# YC Startup Directory Scraper

Scrapes company and founder data from the Y Combinator startup directory.

**No Apify, no API key required.** Uses the free YC public API for company listings and Jina Reader for founder details.

---

## Inputs

| Field | Type | Default | Description |
|---|---|---|---|
| `batch` | string \| null | null | YC batch e.g. `"W25"`, `"S24"`. Null = all batches |
| `industries` | list \| null | null | Client-side industry filter. e.g. `["B2B", "AI"]` |
| `locations` | list \| null | null | Client-side location filter. e.g. `["San Francisco"]` |
| `status` | string \| null | null | `"Active"`, `"Inactive"`, `"Acquired"`, `"Public"` |
| `max_companies` | int | 50 | Max companies returned after filtering |
| `include_founders` | bool | false | Fetch founder name, LinkedIn, bio per company (slower) |

## Outputs

List of companies with:
- `name`, `one_liner`, `description`, `website`, `yc_url`
- `batch`, `status`, `industries`, `tags`, `locations`, `team_size`, `is_hiring`
- `founders[]` — name, linkedin_url, bio (only if `include_founders=true`)

---

## Usage

```bash
python3 scraper.py example_input.json
```

## Notes

- Batch filter is server-side. Industry/location filters are client-side (fetches all batch companies first, then filters).
- `include_founders=true` adds ~5-15s via parallel Jina Reader calls (5 workers).
- All batches mode (`batch=null`) fetches 5,700+ companies across 234 pages — use `max_companies` to cap.
