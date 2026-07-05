# LinkedIn Comment Helper

Finds recent LinkedIn posts worth commenting on, scores each against your project context, and appends results to a rolling Google Sheet. Goal is to surface posts to comment on (build credibility) — not to write the comments themselves.

## Sources

| # | Source | What it pulls | Scraper |
|---|---|---|---|
| 1 | **ICP profiles** | Recent posts from people in your ICP sheet | `LinkedIn Profile Post Scraper` |
| 2 | **Trending in genre** | Posts on broad genre keywords, re-ranked client-side by engagement (replaces a static "famous people" list — surfaces whoever is going viral in your genre right now) | `LinkedIn Post Research` |
| 3 | **Signal** | Posts matching buying-intent phrases (people implementing AI, etc.) | `LinkedIn Post Research` |

## Inputs

| Input | Where it lives |
|---|---|
| ICP profile URLs | Google Sheet — set in `config.json` `icp_sheet.id` |
| Output sheet | Google Sheet — set in `config.json` `output_sheet.id` |
| Project context | `context/context.md` — `## Project` section |
| Genre keywords | `context/context.md` — `## LinkedIn Comment Genre Keywords` (one per line). Falls back to legacy `genre_keywords.json`. |
| Signal keywords | `context/context.md` — `## LinkedIn Comment Signal Keywords` (one per line). Falls back to legacy `signal_keywords.json`. |

If any of `Project`, `LinkedIn Comment Genre Keywords`, or `LinkedIn Comment Signal Keywords` is missing from `context.md`, the workflow prompts you on first run and offers to save your answers back to `context.md`. Use `--auto` to error out instead of prompting (CI/cron).

## Output sheet (rolling, append-only)

Columns: `Run Date` · `Post URL` · `Author` · `Source` · `Posted` · `Why relevant` · `My angle` · `Reactions` · `Comments` · `Status`

- Dedupes by `Post URL` against existing rows so the same post never appears twice.
- Full content is also saved to local `results.json` (URLs + summaries only in the sheet, per repo conventions).

## Modes

- `--mode auto` — runs end-to-end
- `--mode interactive` — pauses after each pull, after scoring, before sheet write
- `--auto` — non-interactive context check (errors out if `context.md` sections are missing instead of prompting). Use for CI/cron.

## Usage

```bash
python3 workflow.py --project "Io Fold" --mode auto
python3 workflow.py --project "Io Fold" --mode interactive

# Skip individual sources
python3 workflow.py --project "Io Fold" --mode auto --skip-icp --skip-trending  # signal only

# Override config at runtime
python3 workflow.py --project "Io Fold" --mode auto --days-back 14 --max-per-keyword 50
```

## Cost & spend preview

This workflow fans out across three paid Apify sources, so a single run costs more than it looks: trending + signal search each run one actor call **per keyword** (`≈ keywords × max_per_keyword` posts at $0.005/post), and the ICP source runs the **profile-posts actor at $5/1,000** — 1,000× the search rate — once per profile. With the default config (7 genre + 5 signal keywords × 25, plus ICP profiles × 10) a run is roughly **$2.50 worst-case** — about half an Apify free-plan month.

Before any actor is billed, the workflow prints an itemized **worst-case spend estimate**. In `--mode interactive` it waits for you to confirm (`y/N`) before spending; in `--mode auto` it prints the estimate for the record and proceeds. Trim `max_per_keyword`, cut keywords, or `--skip-icp` to bring the number down.

## Setup blockers (before first run)

1. Paste your ICP sheet ID into `config.json` `icp_sheet.id` (or pass `--icp-sheet-id`)
2. Paste output sheet ID into `config.json` `output_sheet.id` (or pass `--output-sheet-id`)
3. Fill `context/context.md` with at least `## Project`, `## LinkedIn Comment Genre Keywords`, `## LinkedIn Comment Signal Keywords` — or just run the workflow and let it prompt you on first run.
