# Keyword Validator

Free Google Trends-backed sanity check for SEO keywords.

For each keyword you pass in, returns a relative interest score (0-100, averaged across the timeframe), plus the top 10 related queries and top 10 rising queries that Google Trends associates with it. Useful as a "is anyone actually searching this?" signal for blog SEO targets, content brainstorming, or campaign keyword shortlists.

**This is *relative* interest, not absolute volume.** A score of 80 means "near the peak of this keyword's recent interest", not "80,000 searches/month". For absolute volume, swap the implementation in `scraper.py` for DataForSEO / SerpAPI / Google Ads Keyword Planner — the function signature and return shape are deliberately stable so callers don't change.

## Inputs

```json
{
  "keywords":  ["AI outbound sales", "cold email personalization", "..."],
  "geo":       "US",
  "timeframe": "today 12-m"
}
```

- `keywords`: list of phrases. Required.
- `geo`: ISO country code (e.g. `"US"`, `"IN"`, `"GB"`) or `""` for worldwide.
- `timeframe`: pytrends format. Common values: `"today 12-m"`, `"today 5-y"`, `"now 7-d"`.

## Outputs

```json
{
  "AI outbound sales": {
    "interest_score": 27,
    "related_queries": ["ai sales tools", "ai sales agents", "..."],
    "rising_queries":  ["ai sdr", "claude for sales", "..."],
    "errors": []
  }
}
```

Per-keyword output. If any individual call fails, `errors[]` is populated and other fields are zeroed/empty — the function never raises.

## Usage

```python
from scrapers.keyword_validator.scraper import validate_keywords

scores = validate_keywords(
    keywords=["AI outbound sales", "cold email personalization"],
    geo="US",
    timeframe="today 12-m",
)
for kw, data in scores.items():
    print(f"{kw}: {data['interest_score']}/100")
```

CLI:
```bash
python -m scrapers.keyword_validator.scraper example_input.json
```

## Cost / auth / limits

- **Cost:** free.
- **Auth:** none.
- **Rate limits:** pytrends is *unofficial* — Google rate-limits unauthenticated requests aggressively. You may hit `429` on first call from a fresh IP, or after a burst. The scraper degrades gracefully: 429 ends up in the per-keyword `errors[]` field and downstream callers can decide how to treat it. Retry typically works after a few minutes.
- **Batch size:** Google Trends caps at 5 keywords per request. The scraper batches automatically with a 1s sleep between batches.

## Known limitations

- 429s happen on cold IPs. The scraper does not retry.
- Pytrends sometimes returns empty results for low-volume keywords — interpret 0 as "not enough signal" rather than "definitely no searches".
- Worldwide geo (`""`) tends to be more reliable than country-specific.
- The score is a 12-month average by default — short-lived spikes get smoothed out. Use `timeframe="now 7-d"` to see recent spikes.

## When to upgrade

If you need real monthly search volumes and CPC estimates, you've outgrown pytrends. Drop-in replacements with the same `validate_keywords()` signature:

- **DataForSEO** (`/keywords_data/google_ads/search_volume`) — paid, ~$0.001/keyword
- **SerpAPI Google Trends** — paid, more reliable than pytrends
- **Google Ads Keyword Planner API** — free but requires an active Google Ads account + OAuth

All three give you absolute volume + competition data; switching just means rewriting the body of `validate_keywords()`.
