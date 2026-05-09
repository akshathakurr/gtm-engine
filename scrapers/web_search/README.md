# Web Search Scraper

Fetches structured web results and insights about a company, person, or topic using Exa's semantic search.

## Why Exa over Google/Bing
- Semantic search — understands intent, not just keywords
- Returns per-result summaries and highlights without fetching full pages
- Filters by date, domain, and more natively
- No browser, no proxy — single API call

## Cost
Exa charges per search call + per content fetch. Using `highlights + summary` mode (not full text) keeps costs minimal — typically fractions of a cent per query.

## Inputs

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `query` | string | Yes | — | Company name, person, or topic |
| `num_results` | integer | No | `5` | Results to fetch (1–20). Keep at 5 for efficiency. |
| `days_back` | integer | No | none | Restrict to last N days |
| `include_domains` | array | No | none | Only return results from these domains |
| `exclude_domains` | array | No | none | Skip results from these domains |
| `summary_question` | string | No | generic | Question Exa answers per result from page content |

## Output

```json
{
  "query": "Acko insurance India",
  "total": 5,
  "results": [
    {
      "title": "Acko raises $120M Series D",
      "url": "https://techcrunch.com/...",
      "published_date": "2026-03-15T00:00:00.000Z",
      "author": "Manish Singh",
      "summary": "Acko announced a $120M Series D round led by...",
      "highlights": [
        "Acko, the Indian digital insurance startup, has raised $120M...",
        "The company plans to use the funds to expand into health insurance..."
      ]
    }
  ],
  "errors": []
}
```

## Usage

```bash
# Run with default example input
python3 scraper.py

# Custom input
python3 scraper.py my_input.json
```

## Tips for best results
- **Be specific** — `"Acko insurance India 2026"` beats `"Acko"`
- **Use `summary_question`** — tailor it to what you actually need: `"What has this company raised or announced?"` vs `"What do customers say about this product?"`
- **Use `days_back`** for signal tracking — set to 30 or 90 for recent news only
- **Use `include_domains`** for high-quality sources only: `["techcrunch.com", "forbes.com", "bloomberg.com"]`
- **Keep `num_results` at 5** unless you need broader coverage — 5 results with good summaries beats 20 low-signal ones
