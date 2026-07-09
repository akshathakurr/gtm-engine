# Email Outreach Workflow

End-to-end **email** outreach prep — from a list of companies in either a
**Google Sheet** or a **CSV file**, the workflow enriches every company,
prioritises by ICP fit, then for the top tier (P0 by default) finds the right
buyer, looks up their email via Apollo, scrapes LinkedIn signals, and (once
the relevant skills are wired in) drafts personalised email copy.

```
companies → enrich (ALL) → score ICP/Priority (ALL) → filter P0
                                                         ↓
        find buyer ──────►  classify persona ──────►  find email
                                                         ↓
        scrape posts  ←──  small talk  ←──  personalisation hooks  ──►  email copy
```

The big difference from `linkedin_outreach`: in email outreach the **company is
the unit of qualification**. Person info is optional and added later, only for
companies that qualify — that's how you avoid spending credits on prospects
who'd be filtered out anyway.

---

## What I can fill for you

When someone hands over a raw list and a rough ask ("here are 20 companies, help
me email them"), this is the full menu of what this workflow fills in — read it
out up front so they see what's possible. **By default every column below is
filled; they can ask for a focused subset instead.** One row per company.

- **Company facts** — website, LinkedIn page, one-line description, employee count, estimated revenue, founded year, total funding, HQ, direct competitors
- **Fit & priority** — priority tier, ICP segment, reasoning for the score, buyer-persona match
- **Buyer & contact** — the right buyer at each company and their email (via Apollo)
- **Personalisation** — recent LinkedIn post links, a small-talk opener, a personalisation hook, and ready-to-send email copy

---

## What you need before running

| | Required for |
|---|---|
| Python 3.9+ | always |
| `pip install -r requirements.txt` (run at repo root) | always |
| `ANTHROPIC_API_KEY` in `.env` | every step (LLM reasoning) |
| `EXA_API_KEY` in `.env` | enrichment, buyer search, LinkedIn URL search |
| `APOLLO_API_KEY` in `.env` | Step 6 email lookup |
| `APIFY_API_TOKEN` in `.env` | Step 8 LinkedIn post scraping |
| `gws` CLI installed and authed | only if you use `--sheet-id` |
| `context/context.md` filled in | every run |

Copy `.env.example` → `.env` and fill in keys. Copy
`context/context.md.example` → `context/context.md` and edit it for your
product / ICP.

---

## Quickstart

### Option A — CSV input/output

```bash
python -m workflows.email_outreach.workflow \
  --input-csv  examples/email_outreach/companies.csv \
  --output-csv examples/email_outreach/companies.out.csv
```

Output CSV gets enriched columns appended. Partial progress is flushed after
every write so you can crash mid-run and resume by re-running.

### Option B — Google Sheet

```bash
python -m workflows.email_outreach.workflow \
  --sheet-id   1aBcDeFgH... \
  --sheet-name "Companies"
```

Requires the `gws` CLI installed and authed (`gws auth login` once).

---

## What gets done, step by step

| # | Step | Runs on | What it produces |
|---|---|---|---|
| **1** | Enrich company | **ALL companies** | Web search → Claude extracts: Company URL, Company LinkedIn URL, one-line Company Description, Employee Count, Est Revenue, Founded Year, Total Funding, HQ city, 2-3 Competitors. Deduped per company. Skips rows where every enrichment column is already filled. |
| **2** | Score | ALL companies | ICP Segment + Priority (P0/P1/P2) + 1-line Reasoning. Uses ICP tier definitions from your `context.md`. |
| **3** | Filter | in-memory | Picks the **outreach batch**. **P0 only by default.** Use `--include-p1` / `--include-p2` to widen. Steps 4-10 only touch the batch. |
| **4** | Find buyer | outreach batch | Web search → Claude picks the most relevant person per ICP buyer-persona titles. Writes Name / Position / LinkedIn back to the row. Skips rows where Name is already filled (will still backfill LinkedIn URL if missing). |
| **5** | Classify persona | outreach batch | Decision Maker / Champion / Non Decision Maker for the buyer found in Step 4. |
| **6** | Find email | outreach batch | Apollo Contact Finder → email. Skips rows where email is already filled. |
| **7** | Small Talk | outreach batch | Humanizing/conversational signals (hobbies, fandoms, quirks) — top 2-3 as 1-line bullets with source. Identity-verified per evidence; returns empty rather than wrong-person. |
| **8** | Scrape posts | outreach batch with a LinkedIn URL | Apify pulls each lead's recent posts (default 15 / 90 days). Claude filters by ICP relevance criteria from `context.md`. Writes matching post URLs (newline-separated). |
| **9** | Personalisation hooks | outreach batch | Talking points an SDR can hang an email on. Surfaces 2-3 one-line angles (small talk, matching posts, company signals). See `skills/personalisation_hook`. |
| **10** | Email copy | outreach batch | Final personalised cold email (subject + body + PS). Signal extraction → drafting → self-review → auto-repair if needed. See `skills/email_copy_writer`. |

---

## Inputs — column auto-detection

You don't rename your columns. The workflow asks Claude to map *your* column
names to its internal field keys. Any naming convention works.

The **only required input** is a column with the company name. Everything else
is optional — if missing, the workflow either finds it (Steps 1, 4) or skips
the dependent step (e.g., post scraping needs a LinkedIn URL).

| Workflow uses | Examples it'll detect |
|---|---|
| **company** *(required)* | `Company`, `Company Name`, `Account`, `currentCompany`, etc. |
| **name** | `Name`, `Full Name`, `firstName + lastName`, etc. |
| **linkedin** | `LinkedIn`, `LinkedIn URL`, `linkedinUrl`, etc. |
| **position** | `Position`, `Title`, `Job Title`, `Role`, `headline`, etc. |
| **email** | `Email`, `Work Email`, `email_address`, etc. |

The same auto-detection runs for **output columns** too — if your sheet
already has a column called `Lead Priority`, the workflow writes there
instead of creating a new `Priority` column.

---

## Outputs

If your sheet already has matching columns, the workflow writes into them. If
not, it creates new columns at the end of the sheet using these defaults:

| Default header | Field key | Step |
|---|---|---|
| `Company URL` | `company_url` | 1 |
| `Company LinkedIn URL` | `company_linkedin` | 1 |
| `Company Description` | `company_description` | 1 |
| `Employee Count` | `employee_count` | 1 |
| `Est Revenue` | `est_revenue` | 1 |
| `Founded Year` | `founded_year` | 1 |
| `Total Funding` | `total_funding` | 1 |
| `HQ` | `hq` | 1 |
| `Competitors` | `competitors` | 1 |
| `ICP Segment` | `icp_segment` | 2 |
| `Priority` | `priority` | 2 |
| `Reasoning` | `reasoning` | 2 |
| `Name` | `name` | 4 |
| `Position` | `position` | 4 |
| `LinkedIn Profile` | `linkedin` | 4 |
| `Buyer Persona Match` | `buyer_persona` | 5 |
| `Email` | `email` | 6 |
| `Small Talk` | `small_talk` | 7 |
| `LinkedIn Post Links` | `post_links` | 8 |
| `Personalisation Hook` | `hooks` | 9 |
| `Email Copy` | `copy` | 10 |

### Output formatting (enforced in the LLM prompt)

- **Company URL:** canonical homepage (`https://acme.com`)
- **Company LinkedIn URL:** full LinkedIn page (`https://www.linkedin.com/company/...`)
- **Company Description:** one short, to-the-point line
- **Total Funding / Est Revenue:** absolute number, capital `M` / `B`, **no `~`, no `$`**.
  Examples: `110M`, `4M`, `164.12M`, `1.2B`. Revenue uses `Not available` when unknowable; other fields use empty string.
- **HQ:** city name only (`Oakland`, `Boston`). No state, no country, no street.
- **Founded Year:** 4-digit year (`2014`)
- **Employee Count:** integer or range as stated (`215`, `5,500+`)
- **Competitors:** 2-3 immediate direct competitor names, comma-separated (`Acme, Globex, Initech`)
- **Reasoning:** ONE plain sentence — to the point, no bullets, no filler

---

## Context — what the LLM uses to make decisions

`context/context.md` (your file — gitignored) is the single source of truth.
It's read once and injected into every LLM prompt.

| Section in `context.md` | What it controls |
|---|---|
| **ICP / company profile** | ICP segment assignment + P0/P1/P2 scoring (Step 2) |
| **Decision-maker titles** | who Step 4 picks as buyer; classifies them in Step 5 |
| **Champion titles** | classifies in Step 5 |
| **Disqualifiers** | excluded outright |
| **Tone of voice** | Steps 9 and 10 personalisation copy |
| **`Max posts per profile: N`** *(optional)* | overrides Step 8 default of 15 |
| **`Days back: N`** *(optional)* | overrides Step 8 default of 90 |

See `context/context.md.example` for the full template.

---

## CLI reference

```
python -m workflows.email_outreach.workflow [options]
```

| Flag | What it does |
|---|---|
| `--sheet-id ID` | Read/write a Google Sheet. Mutually exclusive with `--input-csv`. |
| `--sheet-name NAME` | Sheet tab name. Default `Sheet1`. |
| `--input-csv PATH` | Read companies from a CSV. Mutually exclusive with `--sheet-id`. |
| `--output-csv PATH` | Where to write enriched CSV. Defaults to overwriting `--input-csv`. |
| `--limit N` | Process only the first N rows — for quick, cheap test runs. |
| `--add-persona "VP Sales"` | Treat this title as a buyer for *this run only* (repeatable). |
| `--remove-persona "Founder"` | Exclude this title for *this run only* (repeatable). |
| `--enrich-fields KEYS` | Comma-separated subset of enrichment fields, by snake_case key. |
| `--include-p1` | Also include P1 leads in the outreach batch. |
| `--include-p2` | Also include P2 leads in the outreach batch. |
| `--skip-enrich` | Skip Step 1. |
| `--skip-emails` | Skip Step 6 (Apollo). |
| `--skip-small-talk` | Skip Step 7. |
| `--skip-posts` | Skip Step 8. |
| `--skip-hooks` | Skip Step 9. |
| `--skip-copy` | Skip Step 10. |

---

## Examples

```bash
# Companies-only sheet — workflow finds buyers itself for P0s
python -m workflows.email_outreach.workflow \
  --input-csv companies.csv --output-csv companies.out.csv

# Cheap qualification run — enrich + score only
python -m workflows.email_outreach.workflow \
  --input-csv companies.csv \
  --skip-emails --skip-small-talk --skip-posts --skip-hooks --skip-copy

# Cheaper enrichment — only headcount, funding, HQ
python -m workflows.email_outreach.workflow \
  --sheet-id ABC \
  --enrich-fields employee_count,total_funding,hq

# Widen outreach batch beyond just P0
python -m workflows.email_outreach.workflow \
  --sheet-id ABC --include-p1
```

---

## Cost / time rough estimate

For a 100-company run with all default steps and ~20% qualifying as P0:

- Anthropic: ~150-300 LLM calls (column-detection + 100 enrichment + score + per-P0 buyer/persona/post-filter + per-P0 hooks/copy). Roughly **$1-3 with Sonnet 4.6**.
- Exa search: ~120-150 calls (enrichment for all + buyer search for P0 + LinkedIn URL).
- Apollo: ~20 contact lookups (P0 only).
- Apify: ~20 LinkedIn-post-scraping calls × actor cost ($2 per 1,000 posts via HarvestAPI — ~$0.03 per lead at 15 posts).
- Google Sheets API: ~500-1500 cell writes.

Run with `--skip-posts --skip-small-talk --skip-hooks --skip-copy` to limit
cost while qualifying a fresh list.

---

## Known limitations

- **All steps are live.** Steps 9 (`personalisation_hook`) and 10 (`email_copy_writer`) are both built. Step 10 runs a 3-call pipeline: signal extraction → email drafting with self-review committed to JSON → auto-repair pass if any check fails.
- **No `--rows` flag yet.** The whole sheet/CSV is processed every run. Idempotency: enrichment + buyer + emails skip already-filled rows; scoring re-runs unconditionally.
- **No retries.** If Anthropic 429s or Apify throttles mid-run, the script crashes. Anything already written is durable in the Sheet/CSV.
- **`workflow_ops.py` is Sheets-only.** CSV support is in `workflow.py` only.
