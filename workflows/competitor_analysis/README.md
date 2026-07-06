# Competitor Analysis Workflow

Deep competitive intelligence pipeline. Reads competitor names + URLs from a Google Sheet (or CSV) and fills firmographic, product, GTM, and founder columns, then runs a final Claude analysis scoring each competitor against your ICP.

## What I can fill for you

When someone hands over a raw list and a rough ask ("here are 6 competitors,
break them down"), this is the full menu of what this workflow fills in — read
it out up front so they see what's possible. **By default every column below is
filled; they can ask for a focused subset instead.** One row per competitor.

- **Company** — LinkedIn URL, one-line description, employee count, founded year, HQ, recent news
- **Funding** — last funding stage, total funding, estimated revenue
- **Competitive read** — competitor score vs your ICP, key strength, key weakness
- **Founders (top 2)** — name, LinkedIn, Twitter, and what each one posts about
- **Product & GTM** — target persona/user, target ICP, pricing, primary CTA, sales motion, deal size, product features
- **Proof & marketing** — customer stories, customer reviews, top logos, marketing messaging, content type, SEO focus

## Inputs

| Input | Where it lives |
|---|---|
| Competitor list | Google Sheet tab (default `Bird Eye`) — or a CSV file |
| Required columns | `Company Name` (or `Name`) and `Company URL` (or `URL`, `Website`) |
| Project context | `context/context.md` — at minimum `## Product`, `## Ideal Customer Profile`, `## Competitors` |

If your `context.md` is missing the sections needed for the final analysis, the workflow flags them on startup and asks whether to continue (or aborts under `--auto`).

## What it fills

12 steps per row:

| # | Column(s) | How it's populated |
|---|---|---|
| 1 | (cache) | Website scrape — done first, reused across later steps |
| 2 | Company LinkedIn URL | Jina Reader on homepage → co-mention Exa fallback |
| 3 | Company Description | Website scrape + Claude one-liner |
| 4 | Employee Count, Founded Year, Last Funding Stage, Total Funding, Est. Revenue, HQ Location | Exa web search + Claude extraction |
| 5 | Recent News | Exa domain-anchored search + Claude filter |
| 6 | Founder (1/2) Name, LinkedIn, Twitter | Exa founder search → per-founder co-mention LinkedIn lookup |
| 7 | Founder (1/2) Post type | LinkedIn Profile Post Scraper + Twitter Profile Scraper |
| 8 | Target Persona, Sales Motion, Primary CTA, Pricing, Customer Stories, Product Features, Top Logos, Marketing Messaging, SEO | Website scrape + Claude extraction |
| 9 | Customer Reviews | G2 → Trustpilot fallback; writes "not available" if neither found |
| 10 | Deal Size | Exa search + Claude extraction |
| 11 | Content Type | Synthesized from founder posts + company blog |
| 12 | Target ICP, Competitor Score, Strength, Weakness | Final Claude analysis vs. your ICP |

Already-filled cells are skipped — safe to re-run after partial failures.

## Scrapers used

`web_search` (Exa) · `firecrawl_scraper` (primary website scraper — JS-rendered markdown, when `FIRECRAWL_API_KEY` is set) · `website_scraper` (requests + BeautifulSoup + Jina fallback — used when no Firecrawl key) · `linkedin_profile_post_scraper` · `twitter_profile_scraper` · `review_scraper` (G2 + Trustpilot)

> **Website scraping:** company pages (homepage + pricing/about/customers) are fetched via **Firecrawl** when a key is set — it renders JavaScript, so pricing tables and case-study pages that the static scraper missed now come through. Falls back to the static `website_scraper` automatically when no key is present. Firecrawl free tier (1,000 pages/mo) easily covers any competitor run (~6 pages/company).

## Usage

```bash
# Google Sheet
python -m workflows.competitor_analysis.workflow --sheet-id SHEET_ID
python -m workflows.competitor_analysis.workflow --sheet-id SHEET_ID --sheet-name "Bird Eye"
python -m workflows.competitor_analysis.workflow --sheet-id SHEET_ID --only "Acme Corp"

# CSV (no Google auth needed)
python -m workflows.competitor_analysis.workflow --input-csv competitors.csv
python -m workflows.competitor_analysis.workflow --input-csv competitors.csv --output-csv competitors.out.csv

# Skip expensive steps
python -m workflows.competitor_analysis.workflow --sheet-id SHEET_ID --skip-reviews --skip-twitter

# CI / cron — abort if context.md is missing required sections
python -m workflows.competitor_analysis.workflow --sheet-id SHEET_ID --auto
```

## Flags

| Flag | Effect |
|---|---|
| `--sheet-id ID` | Google Sheet ID. Mutually exclusive with `--input-csv`. |
| `--sheet-name NAME` | Sheet tab name. Default: `Bird Eye`. |
| `--input-csv PATH` | Read competitors from a CSV file. |
| `--output-csv PATH` | (CSV only) Where to write output. Defaults to overwriting `--input-csv`. |
| `--only "Name"` | Process only one competitor (exact name match) |
| `--skip-reviews` | Skip G2/Trustpilot scraping (saves Apify credits) |
| `--skip-twitter` | Skip Twitter scraping (saves Apify credits) |
| `--skip-founder-posts` | Skip all founder post scraping |
| `--skip-analysis` | Skip final scoring step |
| `--auto` | Run non-interactively. Errors out if `context.md` is missing required sections. |

## Output formatting (column conventions)

LLM outputs are deliberately constrained so you can scan, filter, and pivot the sheet:

| Column | Format |
|---|---|
| Last Funding Stage | One of: `Pre-Seed`, `Seed`, `Series A`, `Series B`, `Series C`, `Series D`, `Series E+`, `IPO`, `Acquired`, `Bootstrapped`. Convertible notes / SAFEs without a priced round are bucketed as `Seed`. |
| Total Funding | Compact: `10m`, `500k`, `1.2b` |
| Founded Year | 4 digits |
| HQ Location | City only |
| Marketing Messaging | One sentence (~25 words) |
| SEO | One short sentence — `insufficient data — no visible blog/content` if none |
| Content Type | One short sentence — `insufficient data` if none |
| Strength · Weakness | Max 2 lines (~30 words each) — `insufficient data` if none |
| Customer Reviews | Concise summary, or `not available` |

If the LLM doesn't have data, it writes a literal sentinel (`insufficient data` / `not available`) instead of padding with speculation. Grep-friendly.

## Notes

- Already-filled cells are skipped — re-runs are safe.
- LinkedIn URL lookup uses a co-mention Exa query (`{domain} linkedin.com/company`) — more reliable than `site:` filtering.
- Founder LinkedIn lookup uses the same co-mention pattern per founder (`"{name}" "{company}" linkedin.com/in`).
- `site:` operator in Exa is non-deterministic — avoid using it as a hard filter.
- JS-rendered sites (e.g. Framer) may return empty scrapes; fallbacks are in place.
- CSV mode rewrites the output file after every cell write — partial progress survives crashes.
