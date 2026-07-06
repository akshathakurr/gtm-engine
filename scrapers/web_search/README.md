# Web Search Scraper

Fetches structured web results and insights about a company, person, or topic. Backed by **two interchangeable providers** — Exa and Parallel — behind a single `search_web()` interface. Set either or both.

## Providers

| Provider | Key | Notes |
|---|---|---|
| **Exa** | `EXA_API_KEY` | Semantic search; native date + domain filters; `type=fast` (<425ms). |
| **Parallel** | `PARALLEL_API_KEY` | LLM-optimized excerpts; generous free tier. Date/domain filters applied client-side. |

Neither is required individually — you need **at least one**. Both callers and output shape are identical regardless of provider.

### Choosing / combining them (`SEARCH_PROVIDER`)

| Value | Behavior |
|---|---|
| `auto` *(default)* | Use whichever key is set. If **both** are set, use the primary and **fall back** to the other on any failure (rate limit, timeout, exhausted credits). |
| `exa` | Force Exa only. |
| `parallel` | Force Parallel only. |
| `both` | Query both and **merge + dedup** results (broader coverage). |

- `SEARCH_PRIMARY` — when both keys are set, picks the primary: `parallel` (default — spares paid Exa credits and leans on Parallel's free tier) or `exa`.
- `SEARCH_PARALLEL_PROCESSOR` — Parallel tier: `base` (default, fast/cheap, ~$0.005/req) or `pro` (higher quality, 15–60s).

An Exa-only setup (only `EXA_API_KEY` set) behaves exactly as before — `auto` resolves to Exa and Parallel is never touched.

## Cost
Both providers bill per search call and are kept cheap by returning highlights/excerpts rather than full page text — typically fractions of a cent per query. Parallel `base` is ~$0.005 per 10-result call; Exa `fast` is comparable.

## Field mapping
The output shape is identical across providers. For Parallel, `excerpts` become `highlights` (first excerpt seeds `summary`) and `author` is `null` (Parallel does not return it).

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
