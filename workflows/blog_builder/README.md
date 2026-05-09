# Blog Builder Workflow

Three-mode workflow that turns "I want to publish blog posts" into a tracked
pipeline of researched ideas → metadata → draft Google Docs.

```
                  ┌── daily ──────► generate N ideas + research metadata
your goals     ──┼── idea ───────► fill metadata for ideas you typed in
   + references   └── write ──────► turn one researched row into a Google Doc
```

The workflow keeps everything in **one Google Sheet** (one row per blog),
so you can see the funnel at a glance:

```
Idea → Draft Ready → Need Asset → Ready to launch → Published - live
```

## What you need before running

| | Required for |
|---|---|
| Python 3.9+ | always |
| `pip install -r requirements.txt` (run at repo root) | always |
| `ANTHROPIC_API_KEY` in `.env` | every step |
| `EXA_API_KEY` in `.env` | reference + topic research |
| `gws` CLI installed and authed | always (Sheets + Docs) |
| `context/context.md` with **Project**, **Blog Goals & Topics**, **Blog Reference Sources** | every run |

If any of those three sections are empty/missing, the workflow prompts you on
first run and offers to save your answers back to `context.md` so you aren't
re-asked.

## Quickstart

```bash
# Generate 3 fresh ideas based on your goals + references
python -m workflows.blog_builder.workflow --mode daily \
    --sheet-id 1aBcDeFgH... --num-ideas 3

# Research ideas you typed into the sheet manually
python -m workflows.blog_builder.workflow --mode idea \
    --sheet-id 1aBcDeFgH...

# Write the full draft for row 4, drop it in a specific Drive folder
python -m workflows.blog_builder.workflow --mode write \
    --sheet-id 1aBcDeFgH... --row 4 \
    --blogs-folder-id 1O241CcWCY...
```

---

## What gets done, by mode

### `--mode daily` (greenfield idea generation)

1. **Pre-step:** read `context.md`. If `Project`, `Blog Goals & Topics`, or `Blog Reference Sources` is empty, prompt you for it and (with permission) save your answers back to `context.md`.
2. **Derive topic queries** from your Blog Goals & Topics — Claude turns your free-text goals into 5 concrete search queries.
3. **Fetch reference posts** — Exa search on each domain in your Blog Reference Sources (last 90 days, 3 posts each).
4. **Fetch topic posts** — Exa search on the derived queries (last 60 days, 5 results each).
5. **Generate N ideas** — Claude takes refs + topic posts + project context, returns N ideas with `blog_idea`, `talking_points`, `keywords`, `seo_target`, `assets`, `posting_date`, `references`, `why`.
6. (Optional) **Validate keywords** via Google Trends if `--validate-keywords` is set.
7. **Append rows** to the sheet with Status = `Idea`.

### `--mode idea` (research existing ideas)

1. **Pre-step** — same context check.
2. **Read sheet** — pick rows where Blog Idea is filled but Keywords is empty and Status is non-terminal.
3. **For each row:** run Exa for that specific idea (references + topic search), then Claude fills in `talking_points`, `keywords`, `seo_target`, `assets`, `posting_date`, `references`, `why`.
4. (Optional) **Validate keywords** via Google Trends if `--validate-keywords`.
5. **Update the row in place.**

This is the "I have an idea, you research it" path.

### `--mode write` (draft the full post)

1. **Pre-step** — same context check.
2. **Read row N** — needs `Blog Idea`, `Talking Points`, `Keywords`, `SEO Target` filled.
3. **Claude drafts** a 1200–1800 word post, markdown-formatted, with proper H2/H3 structure and the SEO keyword in the right places.
4. **Create a blank Google Doc** (in `--blogs-folder-id` if given, else your root Drive), insert the draft + a metadata header (SEO target, keywords).
5. **Update the row** — `Main Content` column = Doc URL, Status = `Draft Ready`.

---

## Inputs — your `context.md`

Three sections drive this workflow. Copy `context/context.md.example` to
`context/context.md` and fill these in (or let the workflow prompt you):

```markdown
## Project
(One paragraph describing your project: what it does, who it's for, what
problem it solves.)

## Blog Goals & Topics
(Audience, tone, topics you want blogs about, what readers should learn.
Be specific — the more concrete this is, the less generic the output.)

## Blog Reference Sources
Anthropic | anthropic.com
OpenAI | openai.com
Cursor | cursor.com
...
```

## Outputs — sheet schema

The workflow creates these columns in your sheet (header row is enforced
each run — change names here if you want different defaults):

| Column | Field | Filled by |
|---|---|---|
| A | `Blog Idea` | daily (LLM) or you (manual) |
| B | `Why this Blog?` | daily / idea — short 2-line rationale (≤25 words): why it'll rank + who it's for |
| C | `Project Name` | `--project-name` arg (useful when one sheet tracks blogs across multiple projects) |
| D | `Reference` | daily / idea — URLs of inspiration posts |
| E | `Talking Points` | daily / idea — 4-6 concrete bullets |
| F | `Main Content` | write — Google Doc URL |
| G | `SEO Target` | daily / idea — the ONE primary keyword |
| H | `Keywords` | daily / idea — 5-8 SEO keywords |
| I | `Keyword Score` | daily / idea — only filled when `--validate-keywords` is set |
| J | `Assets` | daily / idea — visual / diagram suggestions |
| K | `Status` | mode transitions: `Idea` → `Draft Ready` → `Need Asset` → `Ready to launch` → `Published - live` |
| L | `Posting Date` | daily / idea — suggested publish date (YYYY-MM-DD) |

---

## CLI reference

```
python -m workflows.blog_builder.workflow --mode {daily|idea|write} --sheet-id ID [options]
```

| Flag | Used by | What it does |
|---|---|---|
| `--mode` | all | Required. `daily` / `idea` / `write`. |
| `--sheet-id ID` | all | Required. Google Sheet ID. |
| `--sheet-name NAME` | all | Sheet tab name. Default `Blogs`. |
| `--num-ideas N` | daily | How many ideas to generate. Default 3. |
| `--row N` | write | Required for `write` mode. Sheet row to draft. |
| `--blogs-folder-id ID` | write | Optional. Drive folder to place the new doc in. If omitted, doc lands in your root Drive. |
| `--reference-companies "Name\|domain,..."` | daily / idea | Override Blog Reference Sources for this run only. |
| `--project-name "..."` | daily / idea | Write this string into column C (Project Name) on new rows. |
| `--validate-keywords` | daily / idea | Run keyword_validator scraper (Google Trends) on LLM keywords. Annotates Keyword Score column. |
| `--auto` | all | Run non-interactively. Errors out instead of prompting if context.md is missing required sections. |

---

## Examples

```bash
# First run on a fresh sheet — workflow prompts for missing context
python -m workflows.blog_builder.workflow --mode daily \
    --sheet-id ABC --num-ideas 5

# Once context.md is set, daily runs are quiet
python -m workflows.blog_builder.workflow --mode daily \
    --sheet-id ABC --num-ideas 3 --validate-keywords

# Override references for one specific run
python -m workflows.blog_builder.workflow --mode daily \
    --sheet-id ABC \
    --reference-companies "Stripe|stripe.com,Linear|linear.app,Vercel|vercel.com"

# CI / cron run — no prompts
python -m workflows.blog_builder.workflow --mode daily \
    --sheet-id ABC --auto

# Research ideas you've added by hand
python -m workflows.blog_builder.workflow --mode idea --sheet-id ABC

# Draft the full post for sheet row 4
python -m workflows.blog_builder.workflow --mode write \
    --sheet-id ABC --row 4 --blogs-folder-id 1O241CcWCY...
```

---

## Cost / time rough estimate

For a `--num-ideas 5` daily run with 9 reference companies and 5 topic queries:

- Exa: ~30 search calls (9 references × 3 + 5 topics × 5)
- Anthropic: 2 Claude calls (topic-query derivation + idea generation). ~$0.10-0.30 with Sonnet 4.6.
- Pytrends (only if `--validate-keywords`): up to 5 keywords × 5 ideas = 25 keyword scores via Google Trends. Free, but Google rate-limits aggressively — see `scrapers/keyword_validator/README.md`.
- Google Sheets API: ~5 cell writes.
- Time: typically 30-90s per run.

`--mode write` is more expensive because it drafts a 1200-1800 word post — ~$0.20-0.40 per blog.

---

## Known limitations

- **No image generation.** The Assets column tells you what visuals to make; you make them.
- **Search volume is relative.** `--validate-keywords` uses Google Trends via pytrends, which gives a 0-100 relative interest score, not absolute monthly volume. Swap `scrapers/keyword_validator/scraper.py` for DataForSEO if you need real volume.
- **Pytrends rate-limits.** Google 429s pytrends aggressively from fresh IPs. The scraper degrades gracefully (errors logged per keyword, structurally valid output), but expect occasional empty scores.
- **No publishing.** This workflow stops at "Draft Ready" — you publish manually.
- **No dedupe across runs.** Two `daily` runs may yield overlapping ideas; review before drafting.
- **`--auto` doesn't backfill context.** It errors out instead of prompting; fill `context.md` first.
