# Review Scraper

Single-script review scraper with platform dispatch. Supports G2 and Trustpilot.

---

## Platforms

| Platform | Backend | Cost | Fields |
|---|---|---|---|
| `g2` | Apify `zen-studio/g2-reviews-scraper` | ~$0.003/review | reviewer, title, rating, text, date, verified, incentivized |
| `trustpilot` | Jina Reader (free) | Free | reviewer, rating, text, date + overall rating, total count, AI summary |
| `capterra` | ❌ Not supported | — | Cloudflare blocks all datacenter IPs and Jina |

---

## Inputs

| Field | Type | Required | Description |
|---|---|---|---|
| `platform` | string | ✓ | `"g2"` or `"trustpilot"` |
| `product_url` | string | ✓ | Direct URL to product review page (see format below) |
| `max_reviews` | int | — | Max reviews to return. Default 20. |

**URL formats:**
- G2: `https://www.g2.com/products/{slug}/reviews`
- Trustpilot: `https://www.trustpilot.com/review/{domain}`

---

## Usage

```bash
python3 scraper.py example_input.json
```

## Notes

- G2 requires `APIFY_API_TOKEN` in `.env`. Min 13 items fetched to exceed $0.036 Apify minimum charge.
- Trustpilot via Jina returns ~30 reviews per page (JS-rendered reviews are not available beyond what Jina renders).
- Capterra: every known approach is Cloudflare-blocked. Not buildable on free plan.
