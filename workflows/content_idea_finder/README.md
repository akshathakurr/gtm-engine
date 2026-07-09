# Content Idea Finder

Generates daily content ideas for Twitter and LinkedIn. Pulls signals from three sources, clusters them into topics, and outputs N idea cards classified by genre, content type, and platform. Hook + body are left blank — those are written later by a separate writing skill.

## What I can fill for you

This workflow generates ideas from scratch (no input list needed) — this is the
menu of what each idea card comes with, so you can tell someone what they'll get
back. **Every field below is filled by default.** One row per idea.

- **The idea** — topic, genre, content type, target platform
- **Why & how** — why-now rationale, a suggested angle, the source URLs it came from
- **Tracking** — date and an idea ID

Hook and body are left blank on purpose — those are drafted later by a separate writing skill.

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
| `--sheet-id` | one required | Google Sheet ID |
| `--output-csv` | one required | Write ideas to a local CSV instead (use when you don't want to connect Google) |
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

- Creator pulls (Twitter): ~11 profiles × ~$0.006 (25 tweets at ~$0.25/1k) ≈ **$0.07**
- Trend search (Twitter): ~12 queries × ~$0.006 ≈ **$0.07**
- HN: free
- LLM clustering: ~$0.05
- **Total: ~$0.20/day** if all sources run

Before any Twitter actor is billed, the workflow prints an itemized **worst-case spend estimate** (trends + creators; HN is free). Without `--auto` it waits for a `y/N` confirmation; with `--auto` it prints the estimate and proceeds. Use skip flags during testing to keep the bill down.

## Notes

- Twitter actor (`kaitoeasyapi/...cheapest`) uses public guest tokens — no login required. The workflow inserts a short `time.sleep(2)` between trend queries; the profile batch keeps a 5s gap. The actor rotates tokens internally.
- HN scraper uses Algolia's free API — no quota concerns.
- Source IDs in idea cards (`T3`, `H2`) are internal references the LLM uses to point at supporting signals; they're resolved into full quotes in the local JSON output.
