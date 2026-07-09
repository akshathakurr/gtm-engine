# LinkedIn Comment Helper

Finds recent LinkedIn posts worth commenting on, scores each against your project context, and appends results to a rolling Google Sheet. Goal is to surface posts to comment on (and *why*) — drafting the comment itself is a planned next step.

## What I can fill for you

This is the menu of what the workflow gives you for every post it surfaces, so
you can tell someone what they'll get back. **Every field below is filled by
default.** One row per post worth commenting on.

- **The post** — post URL, author, when it was posted, a one-line summary of what the post is about, current reactions & comments
- **Why it's here** — the source it came from, the intent (prospect vs engagement), why it's relevant to you, and a suggested angle for your comment
- **Tracking** — a status to work the list

Output goes to a Google Sheet, or a **local CSV** (`--output-csv`) when you don't want to connect Google — same columns either way.

Drafting the comment text itself is a planned next step — for now it hands you the post + the angle.

## Two intents — pick one per run

Commenting serves two very different goals, so the workflow runs **one intent per run** (`--intent`, or you're prompted in interactive mode):

| Intent | Goal | Curated input | Discovery input | Ranking |
|---|---|---|---|---|
| **prospect** | Warm up ICPs/prospects you're already reaching out to, so they recognize you and reply | your **ICP list** (profiles) | **signal** phrases — find new strangers showing intent | recency (presence matters) |
| **engagement** | Get reach + engagement on *your own* account by commenting on big voices in your space | your **authority-accounts list** (profiles) | **genre** keywords — find big posts by topic | engagement **velocity** (comment early) |

The comment "angle" the workflow suggests is tailored to the intent: warm-and-personal for prospects, value-add-for-the-audience for engagement.

## Sources

| Source | Used by | What it pulls | Scraper |
|---|---|---|---|
| **Curated profiles** | both | Recent posts from your ICP list (prospect) or authority-accounts list (engagement) | `LinkedIn Profile Post Scraper` |
| **Signal search** | prospect | Posts matching buying-intent phrases (people implementing AI, switching tools, etc.) | `LinkedIn Post Research` |
| **Genre/trending search** | engagement | Posts on broad genre keywords, ranked by engagement velocity | `LinkedIn Post Research` |

## Inputs

| Input | Where it lives |
|---|---|
| ICP profile URLs *(prospect)* | Google Sheet — `config.json` `icp_sheet.id` (or `--icp-sheet-id`) |
| Authority profile URLs *(engagement)* | Google Sheet — `config.json` `authority_sheet.id` (or `--authority-sheet-id`) |
| Output sheet | Google Sheet — `config.json` `output_sheet.id` (or `--output-sheet-id`) |
| Project context | `context/context.md` — `## Project` section |
| Signal keywords *(prospect)* | `context/context.md` — `## LinkedIn Comment Signal Keywords` (one per line). Falls back to legacy `signal_keywords.json`. |
| Genre keywords *(engagement)* | `context/context.md` — `## LinkedIn Comment Genre Keywords` (one per line). Falls back to legacy `genre_keywords.json`. |

On first run the workflow prompts for any missing `context.md` sections **the chosen intent needs** (a prospect run never asks for genre keywords, and vice versa) and offers to save your answers back. Use `--auto` to error out instead of prompting (CI/cron).

## Ranking & filtering

- **Rising over viral** *(engagement)* — authority + genre posts are ranked by *engagement velocity* (reactions+comments ÷ hours since posted, age-floored so brand-new noise can't dominate), not raw engagement, so an early comment still gets seen.
- **Presence** *(prospect)* — ICP posts aren't velocity-culled; any substantive recent post is a chance to be seen by that prospect.
- **Commentable window** — posts with more comments than `commentable_max_comments` (default 150, `0` disables) are dropped as "buried". Applies to every source.
- **Author relevance** — during scoring, Claude also judges whether the *author* is worth being visible to. A post is surfaced only if it's **both** worth commenting on **and** by a relevant author. Curated lists (ICP + authority) are treated as pre-vetted; the gate mainly filters the discovery sources.

## Output sheet (rolling, append-only)

Columns: `Run Date` · `Post URL` · `Author` · `Source` · `Intent` · `Posted` · `Why relevant` · `My angle` · `Reactions` · `Comments` · `Status`

- `Intent` records which play the row came from (`prospect — warm for reply` / `reach — borrow audience`).
- Dedupes by `Post URL` against existing rows so the same post never appears twice.
- Full content is also saved to local `results.json` (URLs + summaries only in the sheet, per repo conventions).

> Note: the `Intent` column is new. On a sheet created before this change, the workflow keeps the existing header and appends in its own column order (it warns you); start a fresh tab for clean alignment.

## Modes

- `--mode auto` — runs end-to-end (requires `--intent`)
- `--mode interactive` — asks for the intent, pauses after each pull, after scoring, before sheet write
- `--auto` — non-interactive context check (errors out if `context.md` sections are missing instead of prompting). Use for CI/cron.

## Usage

```bash
# Prospect play — warm the people you're reaching out to
python3 workflow.py --intent prospect --mode auto
python3 workflow.py --intent prospect --mode interactive

# Engagement play — reach via big accounts + trending topics
python3 workflow.py --intent engagement --mode auto

# Skip a source within the chosen intent
python3 workflow.py --intent prospect --mode auto --skip-signal        # ICP only
python3 workflow.py --intent engagement --mode auto --skip-trending    # authority accounts only

# Override config at runtime
python3 workflow.py --intent engagement --mode auto --days-back 14 --max-per-keyword 50

# Tune the "buried thread" cutoff (0 disables it)
python3 workflow.py --intent engagement --mode auto --commentable-max-comments 50
```

## Cost & spend preview

Each run fans out across **two paid Apify sources** (this intent's curated-profile source + its discovery search). The search actor bills one call **per keyword** (`≈ keywords × max_per_keyword` posts at $0.002/post); the profile-posts actor bills at the same **$2/1,000** rate once per profile. A default prospect run (ICP profiles × 10 + 5 signal keywords × 25) or engagement run (authority profiles × 10 + 7 genre keywords × 25) is roughly **$0.40–0.80 worst-case**.

Before any actor is billed, the workflow prints an itemized **worst-case spend estimate**. In `--mode interactive` it waits for you to confirm (`y/N`) before spending; in `--mode auto` it prints the estimate for the record and proceeds. Trim `max_per_keyword`, cut keywords, or skip a source to bring the number down.

## Setup blockers (before first run)

1. For **prospect** runs: paste your ICP sheet ID into `config.json` `icp_sheet.id` (or pass `--icp-sheet-id`).
2. For **engagement** runs: paste your authority-accounts sheet ID into `config.json` `authority_sheet.id` (or pass `--authority-sheet-id`).
3. Paste output sheet ID into `config.json` `output_sheet.id` (or pass `--output-sheet-id`).
4. Fill `context/context.md` with at least `## Project` plus the keyword section your intent uses — or just run the workflow and let it prompt you on first run.
