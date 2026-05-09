# Content Idea Finder

Generates daily content ideas for Twitter and LinkedIn. Pulls signals from three sources, clusters them into topics, and outputs N idea cards classified by genre, content type, and platform. Hook + body are left blank — those are written later by a separate writing skill.

## Inputs (interactive on first run)

This workflow is driven by four lists. On first run, it checks `context/context.md` for each. If a section is missing, it prompts you. If you skip a prompt, it falls back to built-in defaults and **prints them** so you know exactly what's being used.

| Section in `context.md` | What it controls |
|---|---|
| `## Content Genres` | What topics Claude treats as on-topic. Drives the niche bias of the entire pipeline. |
| `## Content Trusted Creators` | Twitter voices mined for context on each topic. Format `Name \| handle` per line. |
| `## Content Trend Queries` | Twitter search queries (per genre/bucket). Format `bucket \| query` per line. |
| `## Content HN Queries` | Hacker News topics. Format `bucket \| query \| story_type \| sort_by` per line. |

Pass `--auto` for CI/cron — skips prompts and silently uses defaults for any empty section (still prints which defaults are in use).

## Sources

| Source | What | Lookback | Filter |
|---|---|---|---|
| Twitter trends | Keyword search across your trend queries | `--lookback-days` (default 3) | `min_likes` (default 100) |
| Hacker News | Top stories per HN bucket + Show HN | `--hn-lookback-days` (default 7) | `min_points` (default 100) |
| Trusted creators | Recent tweets from your creator list | `--lookback-days` | none |

## Pipeline

1. Twitter trends → keep above min-likes
2. HN → keep above min-points
3. Claude clusters trends + HN into N topics with keywords + `why_now`
4. Pull creator tweets, keyword-match each topic to relevant creator posts
5. Claude classifies each topic → `{genre, content_type, platform, suggested_angle}`
6. Hydrate source IDs to full quotes; write to Google Sheet + `outputs/<date>.json`

## Genres & content types

The Claude classifier uses a fixed taxonomy:

- **Genres:** `Trendy topic` · `Trust building` · `Engineering heavy` · `Project Showcase` · `Engagement`
- **Content types:** `Long form` · `Short-mid` · `Article` · `Blog`
- **Platforms:** `Twitter` · `LinkedIn` · `Both`

## Modes

```bash
# Daily — discover N topics from trends + HN
python -m workflows.content_idea_finder.workflow --mode daily --sheet-id SHEET_ID
python -m workflows.content_idea_finder.workflow --mode daily --sheet-id SHEET_ID --num-ideas 5

# Seed — research a user-supplied idea
python -m workflows.content_idea_finder.workflow --mode idea --sheet-id SHEET_ID \
    --idea "the case for small models"

# CI / cron — no prompts
python -m workflows.content_idea_finder.workflow --mode daily --sheet-id SHEET_ID --auto

# Skip individual sources
python -m workflows.content_idea_finder.workflow --mode daily --sheet-id SHEET_ID \
    --skip-creators --skip-hn
```

## Flags

| Flag | Default | Notes |
|---|---|---|
| `--mode` | required | `daily` or `idea` |
| `--idea` | — | Required when `--mode idea`. Seed string. |
| `--sheet-id` | required | Google Sheet ID |
| `--sheet-name` | `Sheet1` | Tab name |
| `--num-ideas` | 5 | Daily mode only |
| `--lookback-days` | 3 | Creators + Twitter trends |
| `--hn-lookback-days` | 7 | HN window |
| `--skip-creators` / `--skip-trends` / `--skip-hn` | off | Skip individual sources |
| `--auto` | off | Skip prompts; fall back to built-in defaults silently |

## Outputs

**Local JSON** — `outputs/YYYY-MM-DD.json` with full idea cards (text + URLs + author + posted_at per source quote).

**Google Sheet** — one row per idea, URLs only:

| Date | Idea ID | Topic | Genre | Content Type | Platform | Why Now | Suggested Angle | Source URLs | Hook | Body |
|---|---|---|---|---|---|---|---|---|---|---|

`Hook` and `Body` are blank — populated later by a separate writing skill.

## Built-in defaults

If you skip a prompt, these JSON files in this folder supply the fallback values:

- `creators.json` — 11 default creators (tech / startups / AI)
- `trend_queries.json` — 12 default queries (`min_likes: 100`)
- `hn_queries.json` — 4 default buckets (`min_points: 100`)

Edit these to change the defaults; or add your own sections to `context.md` to override per-user.

## Cost estimate (per daily run)

- Creator pulls (Twitter): ~11 profiles × $0.04 ≈ **$0.45**
- Trend search (Twitter): ~12 queries × $0.04 ≈ **$0.48**
- HN: free
- LLM clustering (Sonnet 4.6): ~$0.05
- **Total: ~$1.00/day** if all sources run

Use skip flags during testing to keep the bill down.

## Notes

- Twitter actor (`altimis/scweet`) rate-limits after back-to-back runs. The workflow inserts `time.sleep(30)` between trend queries; profile batch handles rate-limiting internally.
- HN scraper uses Algolia's free API — no quota concerns.
- Source IDs in idea cards (`T3`, `H2`) are internal references the LLM uses to point at supporting signals; they're resolved into full quotes in the local JSON output.
