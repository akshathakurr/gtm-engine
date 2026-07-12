# LinkedIn Outreach Workflow

End-to-end lead qualification and outreach prep — from a list of leads in either
**a Google Sheet** or **a CSV file**, the workflow classifies each person by
buying role, enriches the decision-makers' companies with firmographic data,
prioritises them by ICP fit, scrapes LinkedIn posts, optionally finds
competitors, and (once the relevant skills are wired in) drafts personalised
LinkedIn copy.

```
input  →  classify  →  enrich DMs  →  score P0/P1/P2  →  filter outreach batch
                                                          ↓
                            scrape posts  ←  (competitors, opt-in)  →  small talk
                                                          ↓
                                                  personalisation hooks  →  LinkedIn copy
```

---

## What I can fill for you

When someone hands over a raw list and a rough ask ("here are 20 leads, help me
reach out"), this is the full menu of what this workflow fills in — read it out
up front so they see what's possible. **By default every column below is filled;
they can ask for a focused subset instead.** One row per lead.

- **Company facts** — website, LinkedIn page, one-line description, employee count, estimated revenue, founded year, total funding, HQ
- **Fit & priority** — P0/P1/P2 priority, ICP segment, reasoning for the score, buyer-persona match, likely competitors *(competitors opt-in via `--with-competitors`)*
- **Personalisation** — recent LinkedIn post links, a small-talk opener, a personalisation hook, and a ready-to-send LinkedIn message

---

## What you need before running

| | Required for |
|---|---|
| Python 3.9+ | always |
| `pip install -r requirements.txt` (run at repo root) | always |
| `ANTHROPIC_API_KEY` in `.env` | every step (LLM reasoning) |
| `EXA_API_KEY` (or `PARALLEL_API_KEY`) in `.env` | enrichment (+ competitor lookup with `--with-competitors`) — web search |
| `APIFY_API_TOKEN` in `.env` | post scraping (Step 6) |
| `gws` CLI installed and authed | only if you use `--sheet-id` |
| `context/context.md` filled in | every run — see "Context" section below |

Copy `.env.example` to `.env` and fill in keys. Copy `context/context.md.example`
to `context/context.md` and edit it for your product / ICP.

---

## Quickstart

### Option A — CSV input/output

```bash
python -m workflows.linkedin_outreach.workflow \
  --input-csv  examples/linkedin_outreach/leads.csv \
  --output-csv examples/linkedin_outreach/leads.out.csv
```

The output CSV gets enriched columns appended; partial progress is flushed
after every step, so you can crash mid-run and resume by re-running.

### Option B — Google Sheet

```bash
python -m workflows.linkedin_outreach.workflow \
  --sheet-id   1aBcDeFgH... \
  --sheet-name "Leads"
```

Requires the [`gws`](https://github.com/marlonbarrios/gws) CLI installed and
authed against a Google account that can read/write the sheet
(`gws auth login` once).

---

## What gets done, step by step

| # | Step | Runs on | What it produces |
|---|---|---|---|
| **Pre** | Find missing inputs | rows missing name / title / company | Web-searches whatever data you have (LinkedIn URL, partial name) and lets Claude fill the gaps. Rare. |
| **1** | Classify | every lead | `Decision Maker` / `Champion` / `Non Decision Maker` based on title + your `context.md`. Written to the buyer-persona column. |
| **2** | Enrich | **Decision Makers only** | Web search → Claude extracts: Company URL, Company LinkedIn URL, Company Description (one-liner), Employee Count, Est Revenue, Founded Year, Total Funding, HQ. **Deduped per company** so two DMs from the same firm only cost one search. **Skips rows whose enrichment columns are already filled.** |
| **3** | Score | every lead | `P0` / `P1` / `P2` + ICP Segment + 1-2 sentence reasoning. Uses ICP tier definitions from your `context.md` if present; otherwise general fit signals. |
| **4** | Filter | in-memory | Picks the **outreach batch**. **P0 only by default.** Use `--include-p1` and/or `--include-p2` to widen. Steps 5-9 only touch the batch — not the full sheet. |
| **5** | Competitors *(opt-in)* | outreach batch | **Skipped by default** — pass `--with-competitors` to enable. When on: 2-3 immediate direct competitors per lead's company (one extra web search each), cached per company. |
| **6** | Scrape posts | outreach batch | Apify pulls each lead's recent LinkedIn posts (default: 15 posts, last 90 days). Claude filters by ICP relevance criteria from `context.md`. Writes the matching post URLs (newline-separated). |
| **7** | Small Talk | outreach batch | Humanizing/conversational signals (hobbies, fandoms, quirks) — top 2-3 as 1-line bullets with source. Identity-verified per evidence; returns empty rather than wrong-person. |
| **8** | Personalisation hooks | outreach batch | Talking points an SDR can hang a message on. Surfaces 2-3 one-line angles from small talk, matching posts, and company signals. See `skills/personalisation_hook`. |
| **9** | LinkedIn copy | outreach batch | Final personalised LinkedIn DM — a short, human, conversation-starting message built around the strongest signal, with an auditable three-question self-review and one repair pass. See `skills/linkedin_copy_writer`. |

---

## Inputs — column auto-detection

You don't rename your columns. The workflow asks Claude to map *your*
column names to its required fields. Any naming convention works:

| Workflow needs | Examples it'll detect |
|---|---|
| **name** | `Name`, `Full Name`, `firstName + lastName`, `Champion First Name + Champion Last Name`, etc. |
| **company** | `Company`, `Company Name`, `currentCompany`, etc. |
| **linkedin** | `LinkedIn`, `LinkedIn URL`, `linkedinUrl`, `Champion LinkedIn`, etc. |
| **position** | `Position`, `Title`, `Job Title`, `Role`, `currentTitle`, `headline`, etc. |

If first and last name are split across two columns, the workflow combines them
automatically.

The same auto-detection runs for **output columns** too — if your sheet already
has a column called `Lead Priority`, the workflow writes there instead of
creating a new `Priority` column.

---

## Outputs

If your sheet already has matching columns, the workflow writes into them. If
not, it creates new columns at the end of the sheet using these default names:

| Default header | Field key | Step |
|---|---|---|
| `Buyer Persona Match` | `buyer_persona` | 1 |
| `Company URL` | `company_url` | 2 |
| `Company LinkedIn URL` | `company_linkedin` | 2 |
| `Company Description` | `company_description` | 2 |
| `Employee Count` | `employee_count` | 2 |
| `Est Revenue` | `est_revenue` | 2 |
| `Founded Year` | `founded_year` | 2 |
| `Total Funding` | `total_funding` | 2 |
| `HQ` | `hq` | 2 |
| `Priority` | `priority` | 3 |
| `ICP Segment` | `icp_segment` | 3 |
| `Reasoning` | `reasoning` | 3 |
| `Competitors` *(opt-in)* | `competitors` | 5 |
| `LinkedIn Post Links` | `post_links` | 6 |
| `Small Talk` | `small_talk` | 7 |
| `Personalisation Hook` | `hooks` | 8 |
| `LinkedIn Copy` | `copy` | 9 |

### Output formatting (enforced in the LLM prompt)

- **Company URL:** canonical homepage (e.g. `https://acme.com`)
- **Company LinkedIn URL:** full LinkedIn page (`https://www.linkedin.com/company/...`)
- **Company Description:** one short, to-the-point line about what the company does
- **Total Funding:** absolute number, capital `M` for millions or `B` for billions, **no `~`, no `$`**.
  Examples: `110M`, `4M`, `164.12M`, `1.2B`
- **Est Revenue:** same format. All values USD — never include `$`.
  If not credibly knowable: `Not available`
- **HQ:** city name only (`Oakland`, `Boston`, `San Francisco`). No state, no country, no street.
- **Founded Year:** 4-digit year (`2014`)
- **Employee Count:** integer or range as stated (`215`, `5,500+`)

---

## Context — what the LLM uses to make decisions

`context/context.md` (your file — gitignored) is the single source of truth.
It's read once and injected into every LLM prompt. The relevant sections:

| Section in `context.md` | What it controls |
|---|---|
| **Decision-maker titles** | which titles classify as DM in Step 1 |
| **Champion titles** | which titles classify as Champion in Step 1 |
| **Disqualifiers** | excluded outright |
| **ICP / company profile** | ICP segment assignment + P0/P1/P2 scoring |
| **Tone of voice** | Steps 8 and 9 personalisation copy |
| **`Max posts per profile: N`** *(optional line)* | overrides Step 6 default of 15 |
| **`Days back: N`** *(optional line)* | overrides Step 6 default of 90 |

If a section is missing, the LLM falls back to general B2B conventions —
classification still works but quality drops. Fill in `context.md` before any
real run.

See `context/context.md.example` for the full template.

---

## CLI reference

```
python -m workflows.linkedin_outreach.workflow [options]
```

| Flag | What it does |
|---|---|
| `--sheet-id ID` | Read/write a Google Sheet. Mutually exclusive with `--input-csv`. |
| `--sheet-name NAME` | Sheet tab name. Default `Sheet1`. |
| `--input-csv PATH` | Read leads from a CSV. Mutually exclusive with `--sheet-id`. |
| `--output-csv PATH` | Where to write the enriched CSV. Defaults to overwriting `--input-csv`. |
| `--limit N` | Process only the first N leads — for quick, cheap test runs. |
| `--add-persona "VP Sales"` | Treat this title as a buyer for *this run only* (repeatable). |
| `--remove-persona "Founder"` | Exclude this title for *this run only* (repeatable). |
| `--enrich-fields KEYS` | Comma-separated subset of enrichment fields, by snake_case key (e.g. `employee_count,total_funding,hq`). Defaults to all. |
| `--include-p1` | Also include P1 leads in the outreach batch. |
| `--include-p2` | Also include P2 leads in the outreach batch. |
| `--skip-enrich` | Skip Step 2. |
| `--with-competitors` | **Opt in** to competitor lookup (Step 5). Skipped by default — one extra web search per company. |
| `--skip-posts` | Skip Step 6. |
| `--skip-small-talk` | Skip Step 7. |
| `--skip-copy` | Skip Step 9. |

---

## Examples

```bash
# Smallest run — just classify + score (no enrichment, no posts, no copy)
python -m workflows.linkedin_outreach.workflow \
  --input-csv leads.csv --output-csv leads.out.csv \
  --skip-enrich --skip-posts --skip-small-talk --skip-copy

# Override personas one-off
python -m workflows.linkedin_outreach.workflow \
  --sheet-id ABC --sheet-name "Q2 Leads" \
  --add-persona "VP Sales" --remove-persona "Founder"

# Cheaper enrichment — only headcount, funding, HQ
python -m workflows.linkedin_outreach.workflow \
  --input-csv leads.csv \
  --enrich-fields employee_count,total_funding,hq

# Widen outreach batch beyond just P0
python -m workflows.linkedin_outreach.workflow \
  --sheet-id ABC --include-p1
```

---

## Cost / time rough estimate

For a 100-lead run with all default steps enabled and ~30% Decision Makers:

- Anthropic: ~120-220 LLM calls (1 column-detection + 1 classify + 30 enrichment + 1 score + ~30 post-filter + per-lead hooks/copy when wired; +~30 competitor calls only with `--with-competitors`). Roughly **$1-3 with Sonnet 4.6**.
- Exa search: ~30-60 calls (enrichment; +competitors only with `--with-competitors`). Per Exa free-tier limits.
- Apify: ~30 LinkedIn-post-scraping calls × actor cost ($2 per 1,000 posts via HarvestAPI — ~$0.03 per lead at 15 posts).
- Google Sheets API: ~500-1500 cell writes — usually fine on quotas.

Run with `--skip-posts --skip-small-talk --skip-copy` to limit cost while
testing. The pre-step (find missing inputs) only fires for rows missing a
required field, so on clean input it costs nothing.

---

## Interactive mode (`workflow_ops.py`)

`workflow_ops.py` is a separate entrypoint for driving the workflow from inside
a Claude Code session. Instead of the script doing all reasoning, you keep
Claude open and it calls these subcommands as data ops:

```
read-sheet      → dump all leads as JSON
web-search      → run a search query, return raw results
scrape-posts    → pull LinkedIn posts for a profile URL
write-column    → write a full column (JSON array of values)
write-cell      → write a single cell (A1 notation)
```

Useful when you want to mix-and-match steps, retry just one row, or have
Claude reason aloud. Sheets-only today; no CSV mode for `workflow_ops.py` yet.

---

## Known limitations

- **All steps are live.** Steps 7 (Small Talk), 8 (Personalisation Hook), and 9 (LinkedIn copy) are all wired in. Step 9 still gates gracefully with a try/import so a broken dependency skips rather than crashes the run.
- **No `--rows` flag yet.** The whole sheet/CSV is processed every run.
  Idempotency: enrichment skips already-filled rows, but classify/score/competitors
  re-run unconditionally — re-runs cost the LLM bill again.
- **No retries.** If Anthropic 429s or Apify throttles mid-run, the script
  crashes and any pending writes are lost (but anything already written is
  durable in Sheets/CSV).
- **`workflow_ops.py` is Sheets-only.** CSV support is in `workflow.py` only.
