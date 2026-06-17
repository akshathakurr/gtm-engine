# Learnings

Mistakes made and lessons learned while building scrapers and workflows. Read this at the start of every new chat.

---

## Scraper Build Process

**Always do a discovery run before writing any parser.**
Run 1 item against the Apify actor, dump the raw JSON, save it as `raw_sample.json` in the scraper folder. Read it first. Only then write field mappings in `scraper.py`. Skipping this wastes time fixing wrong field names after the fact.

**Apify URL input format.**
All Apify actors expect URLs as an array of objects: `[{"url": "https://..."}]` — not a flat list of strings `["https://..."]`. This is consistent across actors.

**Python version is 3.9.**
Do not use `X | None` union syntax for type hints — it requires Python 3.10+. Always use `Optional[X]` from `typing` instead.

---

## Scraper Folder Checklist

Every scraper folder must have these 7 files before it's considered done:
- `scraper.py`
- `input_schema.json`
- `output_schema.json`
- `example_input.json`
- `example_output.json`
- `raw_sample.json` ← real raw response from the actor, used to verify field names
- `README.md`

---

## Scrapers Built

### LinkedIn Profile Scraper ✅
- **Apify actor:** `supreme_coder/linkedin-profile-scraper`
- **Cost:** $0.003/profile
- **Key fields:** `inputUrl`, `firstName`, `lastName`, `headline`, `summary`, `geoLocationName`, `jobTitle`, `companyName`, `positions[]`, `educations[]`
- **Position date format:** `timePeriod.startDate` / `timePeriod.endDate` → `{year, month}`
- **No cookies required**

---

## Skill Build Patterns

**Skills are folders, not single files.**
`skills/<name>/skill.py` + `__init__.py` + `README.md`. Workflow imports `from skills.<name> import skill`. The `skills/README.md` used to say single `.md` file — that was wrong and has been corrected.

**Only encode rules the user explicitly stated.**
Easy to add extra style/tone constraints that feel sensible but weren't asked for (e.g. "never start with I"). These silently change behavior in ways the user didn't expect. If a rule feels like an improvement, ask first rather than baking it in. Let the user's own examples guide tone — don't layer extra constraints on top.

**Force self-review into the JSON, don't just ask for it.**
Telling a model "self-review before outputting" doesn't reliably work — the model claims it did without actually changing anything. Committing answers into a structured `review` field changes behavior: the model has to decide on concrete values ("no" / "yes" / empty list) and that act of commitment makes the check real. Gate on the field values in Python so violations are surfaced to the caller.

**Auto-repair pattern: one extra call, only when needed.**
If self-review flags violations, send the draft back once with the specific issues listed and ask for a fix. Cap at one retry to keep cost bounded. After repair, re-audit and log anything still failing as `unresolved: ...` — don't silently swallow persistent issues.

**Gate on the verdict token, not an exact-string match.**
When auditing a `review` field, the model often appends a justification to the verdict (e.g. `"no — quotes his own post back to him"` instead of bare `"no"`). An exact `value != "no"` check then fires a false repair on a perfectly good draft. Normalize to the leading word before comparing (`re.split(r"[^a-z]+", value.lower())[0]`). Bonus: the justification is *more* auditable than a bare token, so keep it in the stored `review`. Both `linkedin_copy_writer` and `email_copy_writer` use this `_leading_token()` helper in `_audit_copy()`. (List-valued checks like `banned_phrases_used` stay exact — empty list = pass.)

**Signal extraction as a separate call.**
When a skill needs to find the best angle from a pile of data, extract the signal in its own call before writing the copy. This separates "what to say" from "how to say it" and produces better results than asking one call to do both. The signal call is cheap (max_tokens=500); the writing call can then focus on execution.

**Empty-over-padding applies to skills too.**
Same rule as scrapers: if input signal is weak, return empty + error rather than generic output. A lean company-level email with an error flag beats a fake-personalised one. The caller decides whether to retry, skip, or surface the gap.

---

## GWS (Google Workspace CLI)

- Installed at `~/.npm-global/bin/gws`
- Auth can expire — if you get a 401, run `gws auth login` to re-authenticate
- To avoid shell escaping issues when passing large JSON bodies, write the payload to `/tmp/` first and read it via Python subprocess instead of passing inline

---

## Workflow patterns

These are the patterns that came out of testing `linkedin_outreach` end-to-end.
Apply them to every other workflow to make it OSS-ready.

### 1. Never hard-code column names — use LLM column detection

Sheets in the wild have wildly different conventions: `firstName`, `Champion First Name`, `Lead First Name`, `Contact: Given Name`, etc. Don't try to enumerate aliases. Send headers + first data row to Claude and have it return a mapping.

```python
def detect_columns(headers, sample_row, required_fields, client) -> Dict[str, List[int]]:
    """required_fields = {"name": "Full person name (combine first+last if split)", ...}
       Returns {field_name: [col_idx, ...] or []}.
       Multiple indices means combine (e.g. firstName + lastName)."""
```

Pair with:

```python
def cell_combined(row, indices):
    return " ".join(row[i].strip() for i in indices if i < len(row) and row[i].strip())

def get_or_create_col(headers, mapping, field_key, default_name):
    """Return existing col idx if mapping found one; else append new col."""
    if mapping.get(field_key):
        return mapping[field_key][0]
    return ensure_col(headers, default_name)
```

**Run column detection ONCE at the top of `main()` for both inputs AND outputs.** Pass every field the workflow reads or writes. Then use `cell_combined(row, mapping[key])` for reads and `get_or_create_col(headers, mapping, key, default)` for writes.

### 2. Snake_case keys are canonical; display labels are defaults

Define each output as a list of dicts with `key`, `label`, `desc` — single source of truth:

```python
ENRICH_FIELDS = [
    {"key": "company_url",      "label": "Company URL",       "desc": "Company website URL"},
    {"key": "employee_count",   "label": "Employee Count",    "desc": "Headcount"},
    {"key": "total_funding",    "label": "Total Funding",     "desc": "Total raised"},
    ...
]
```

The LLM extraction prompt uses `key` as the JSON key. The sheet write uses `mapping[key]` to find the user's existing column, falling back to `label` only when none exists. Add a new field by adding one entry — nothing else changes.

### 3. Backend abstraction for Sheets vs CSV

Don't lock workflows to Google Sheets. Build two classes with the same 4 methods so the workflow body is identical for both:

```python
class GoogleSheetsBackend:
    def read_all() -> List[List[str]]
    def write_header(col_idx, name)
    def write_cell(row_num, col_idx, value)
    def write_column(col_idx, values)

class CsvBackend:
    # same interface; flushes the full CSV after every write so crashes don't lose progress
```

CLI: `--sheet-id` and `--input-csv` are a mutually-exclusive required group. Add `--sheet-name` (Sheets only) and `--output-csv` (CSV only) as optional siblings.

### 4. Pre-step: fill in missing inputs via web search

Before classification, check each lead for missing required fields. If anything's blank, web-search using whatever the lead does have (LinkedIn URL, partial name, company) and let Claude extract the missing fields. Only fires for incomplete rows — costs nothing on clean input.

```python
needed = [k for k in REQUIRED if not (lead.get(k) or "").strip()]
if not needed: return lead
query = " ".join(filter(None, [lead.get("linkedin"), lead.get("name")]))
# search → extract via Claude → fill `needed` keys only
```

### 5. Enforce strict formatting in extraction prompts

LLMs return free-form noise unless you spell out exactly what you want. For every enrichment field, pin down:

- **Funding / revenue:** `"110M"` / `"4M"` / `"1.2B"`. **No `~`, no `$`, no `million`/`bn` words.** All values USD.
- **Revenue when uncertain:** `"Not available"` (other fields use empty string).
- **HQ:** city only — `"Oakland"`, `"Boston"`. No state, no country, no street.
- **Founded year:** 4 digits (`"2014"`).
- **Headcount:** integer or range as stated (`"215"`, `"5,500+"`).
- **Description:** one short, to-the-point line.

Put these rules in the prompt verbatim, with examples. Don't post-process — make the LLM produce the canonical form.

### 6. Scope expensive steps narrowly

- **Enrichment:** Decision Makers only. Champions and Non-DMs cost LLM + Exa calls without ROI.
- **Outreach batch (Step 4):** P0 only by default. Provide opt-in flags (`--include-p1`, `--include-p2`) so the user can widen on demand.
- **Cache by company name** — two DMs from the same firm should only cost one search.
- **Skip rows already filled** — re-running shouldn't re-pay the bill for completed work.

### 7. Common bugs to avoid

| Bug | Fix |
|---|---|
| In-memory cache referenced before assignment across step boundaries | Declare all step-spanning dicts at top of `main()`, before Step 1. |
| Header overwrite — workflow writes its default name over the user's existing column | When writing the header cell, use `headers[idx]` (which is either the user's existing name or our just-appended default). Never use a literal string. |
| `.env` changes ignored | `load_dotenv(path, override=True)` in `config.py`. Shell env vars shadow `.env` otherwise. |
| Field renames break callers | When refactoring (e.g. add `icp_segment` to `score_leads()` output), update every caller's tuple unpacking — easy to miss. Run `python -m workflows.X.workflow --help` after every refactor. |

### 8. Stub-skip pattern for unbuilt scrapers/skills

Workflows often reference scrapers or skills that aren't built yet. Don't crash — gate with a try/import and a `_AVAILABLE` flag:

```python
try:
    from skills.personalisation_hook import skill as _hook_skill
    _HOOK_AVAILABLE = True
except Exception:
    _hook_skill = None
    _HOOK_AVAILABLE = False

# later:
if not _HOOK_AVAILABLE:
    print("--- Step 8: Personalisation Hook skill not built — skipping ---")
```

This way the workflow can be published with stubs, and steps light up as you build them.

### 9. CLI conventions

- `--skip-X` for every optional/expensive step.
- `--include-pN` to widen filtered batches (default: tightest).
- `--enrich-fields key1,key2` for narrowing what gets enriched.
- `--add-persona`, `--remove-persona` (action="append") for one-time overrides without editing `context.md`.
- Print "Step N: ..." headers liberally — the user is watching a long-running CLI.

### 10. Per-workflow self-contained code

No shared helpers. Each workflow folder has its own copy of `gws_read_sheet`, `gws_write_range`, `_strip_json_fence`, `classify_personas`, `score_leads`, `find_competitors`, `enrich_company`, the two backends, etc. ~150-300 lines duplicated per workflow — accepted cost for being able to read/fork/copy a single folder in isolation.

### 11. Interactive context-backfill pre-step

Some workflows (`blog_builder`, anything that needs project descriptions / blog goals / per-project preferences) need free-form context that won't fit ICP scoring patterns. For those:

```python
REQUIRED_SECTIONS = [
    {"key": "project",    "header": "Project",                "prompt": "..."},
    {"key": "goals",      "header": "Blog Goals & Topics",    "prompt": "..."},
    {"key": "references", "header": "Blog Reference Sources", "prompt": "...", "multiline": True},
]

def ensure_context_complete(auto: bool) -> Dict[str, str]:
    # Walk the list. For each missing/empty section in context.md:
    #   if --auto: print error + sys.exit(2)
    #   else:      input() prompt → optionally append back to context.md
    ...
```

Parse markdown sections with a single regex:
```python
def _section_body(text: str, header: str) -> str:
    pattern = rf"(?ms)^##\s+{re.escape(header)}\s*$\n(.*?)(?=^##\s+|\Z)"
    m = re.search(pattern, text)
    return m.group(1).strip() if m else ""
```

Treat known placeholders (`(fill this in)`, `(none)`, `(skip)`) as empty so users can leave the example template intact and still trigger a prompt.

After collecting answers, ask once: `Save these answers to context/context.md? [Y/n]`. If yes, append a fresh `## Header` block per missing section.

Always provide `--auto` for CI / cron usage — errors out cleanly if context is incomplete instead of hanging on `input()`.

### 12. Schema-divergence guard on existing sheets

`ensure_headers()` should NOT blindly rewrite the header row when it differs from `SHEET_HEADERS`. If the sheet already has data rows, rewriting the header silently misaligns existing values under new column meanings — destructive, hard to spot.

```python
def ensure_headers(sheet_id, sheet_name):
    rows = gws_read_sheet(sheet_id, sheet_name)
    if not rows:
        gws_append_rows(sheet_id, sheet_name, [SHEET_HEADERS])
        return
    if rows[0] == SHEET_HEADERS:
        return
    if len(rows) > 1:
        # Existing data — keep their header, print a warning
        print(f"  Note: '{sheet_name}' has data rows under a different header schema. "
              f"Keeping existing header; new rows append with workflow column order.")
        return
    gws_update_row(sheet_id, sheet_name, 1, SHEET_HEADERS)  # only rewrite when sheet is empty
```

Pair with named column-index constants (`COL_IDEA, COL_WHY, COL_PROJECT, ...`) defined right next to `SHEET_HEADERS` so position changes are a one-line edit and grep is reliable.

### 13. Free / unauthenticated APIs degrade — code for it

Some scrapers wrap APIs that work most of the time but rate-limit / 429 from fresh IPs (pytrends → Google Trends, anything scraping search engines). Don't crash; return the same shape with errors recorded per item:

```python
return {
    keyword: {
        "interest_score":  0,
        "related_queries": [],
        "rising_queries":  [],
        "errors": ["..."]    # caller decides whether to retry, ignore, or surface
    }
    for keyword in keywords
}
```

The caller (workflow) decides whether to retry or surface. The user sees structurally valid output even on a degraded run, which beats "the whole step crashed".

Also: keep the *raw* response (including the 429 case) as `raw_sample.json` — that file should depict reality, not the happy path. Use `example_output.json` for the happy path.

### 14. Stable function signature for swappable backends

Keyword validation, contact finding, search APIs — the cheap implementations differ from the paid ones, but callers shouldn't change when you upgrade. Pick the function shape so swap is local:

```python
# scrapers/keyword_validator/scraper.py
def validate_keywords(keywords, geo="", timeframe="today 12-m") -> dict:
    """Returns {keyword: {interest_score, related_queries, rising_queries, errors}}"""
```

Today the body is `pytrends`. Tomorrow it could be DataForSEO / SerpAPI / Google Ads — the workflow that calls `validate_keywords()` doesn't know or care. Document the swap path in the scraper's README.

Same pattern applies to `find_email()` (Apollo today, could be Hunter / Findymail), `search_web()` (Exa today, could be SerpAPI), etc.

### 15. Don't hardcode niche assumptions inside LLM prompts

Easy trap when copying prompts between projects: the prompt assumes a specific industry / audience that was true for the author but is wrong for any other user. Example caught in `linkedin_comment_helper`:

```python
# BAD — bakes niche into the prompt
prompt = "You are helping me decide which LinkedIn posts to comment on to build credibility in B2B SaaS / GTM / AI..."

# GOOD — let the user's project_context drive genre inference
prompt = "You are helping me decide which LinkedIn posts to comment on to build credibility in my space. Use my project context below to infer my genre, audience, and what kinds of posts are relevant — do NOT assume any specific industry.\n\nMy project context: {project_context}"
```

Rule: any concrete noun in the LLM prompt (industry, persona, geography, tone, framework name) that isn't a *workflow constant* should come from user context, not be hardcoded. Grep prompts for industry words ("SaaS", "B2B", "founder", "developer", "AI") before publishing — if they're not derived from context, they're bias.

### 16. Sheet-bound LLM outputs need explicit length limits + canonical enums + an empty-data sentinel

Sheets are not paragraphs. When an LLM fills a column, three things go wrong by default:

1. **Bloat.** Asked for "2-3 sentences", you get 5. Asked for "key strengths", you get a paragraph.
2. **Free-form values where you wanted enums.** "Last Funding Stage" comes back as "Convertible Note" or "Pre-priced SAFE" instead of `Seed`/`Series A` — useless for filtering downstream.
3. **Hallucinated padding when data is missing.** Asked about a competitor with no blog, the LLM writes three speculative sentences about their "likely SEO strategy" instead of admitting it doesn't know.

Fix all three in the prompt:

```python
# BAD
- "Last Funding Stage": stage name (e.g. "Seed", "Series A")
- "SEO": 2-3 sentences on their content/SEO strategy
- "Strength": 2-sentence summary of their key advantages

# GOOD
- "Last Funding Stage": MUST be one of: "Pre-Seed", "Seed", "Series A", "Series B", "Series C", "Series D", "Series E+", "IPO", "Acquired", "Bootstrapped". If only a convertible note / SAFE exists with no priced round, use "Seed". Never invent new categories.
- "SEO": ONE short sentence (~20 words). If no visible blog/content, write exactly "insufficient data — no visible blog/content". Never pad.
- "Strength": MAX 2 short lines (~30 words). Punchy, specific, no preamble. If no signal, write "insufficient data".
```

Three rules to bake in:
- **Length.** "ONE sentence (~20 words)" / "MAX 2 lines (~30 words)" — measurable. "Concise" / "short" doesn't constrain.
- **Enums.** "MUST be one of [...]. Never invent new categories." Spell out the canonical list. Without that, downstream filters silently miss rows.
- **Empty-data sentinel.** A literal string like `"insufficient data"` (or `"not available"`) the user can grep for. The LLM defaults to filling silence with prose unless you give it a graceful out.

### 17. Backfill pattern when defaults already exist: prompt → skip → show defaults

Pattern #11 (interactive context-backfill) assumed the workflow had no defaults — every required section had to be filled. But many workflows ship with reasonable built-in defaults (creators list, trend queries, HN buckets, default ICP keywords). If you force users through #11's full prompt loop on every section, they'll skip past defaults they didn't even know existed.

The fix: same prompt-and-save-back loop, but if the user skips a section, **print the built-in defaults that will be used** so they understand exactly what just happened.

```python
# Per-section resolution:
#   1. context.md has the section?    → parse and use it
#   2. else --auto?                    → silently use built-in default + print it
#   3. else prompt (multiline)
#        · user provided?              → parse, use, queue for save-back
#        · user pressed Enter (skip)?  → use built-in default + PRINT what it is

print(f"  Using built-in defaults for [{spec['header']}]:")
print(f"    {spec['default_label']}")  # short comma-joined preview, e.g. "Paul Graham, Sam Altman, ... (+8 more)"
```

Why printing matters: under #11, a skipped section would have produced empty output ("no creators configured"). With defaults, skipping silently activates someone else's opinion (the workflow author's). Printing closes that loop — the user sees what was just chosen on their behalf and can override next time by editing `context.md`.

The save-back prompt only runs if the user *answered* at least one section. Skipped sections never trigger save-back (nothing to save).

### 18. Structured config in context.md: pipe-delimited lines, not JSON

When a workflow input is more than a flat list — creators with handles, search queries with buckets, HN topics with sort/type — keep `context.md` human-editable by using one-line, pipe-delimited records:

```markdown
## Content Trusted Creators
Paul Graham | paulg
Sam Altman | sama

## Content Trend Queries
startups | seed funding announcement
ai_building | LLM evals

## Content HN Queries
ai_building | AI OR LLM OR agent | story | relevance
showcase    |                    | show_hn | date
```

Parsing rules:
- Empty lines, lines starting with `#`, `(`, `-` → skipped (treat as comments / scaffolding)
- `|` is the delimiter; each parser knows its expected field count
- Trailing optional fields default to sensible values (e.g. HN `story_type` defaults to `"story"`, `sort_by` to `"relevance"`)
- A line that doesn't match the expected shape is silently skipped — never crash on a typo'd line

Don't make users edit JSON inside `context.md` — they'll bracket-mismatch and rage-quit. A one-line-per-record format is forgiving, diff-friendly, and trivially regenerated when you save back from a multiline prompt.

### 19. Questionnaire-as-interface: don't build a setup skill, write a good context.md.example

When you need to interview a user for context, the temptation is to build a `/setup-context` skill or CLI wizard. Don't. Claude Code already runs an interview if the file it's reading is shaped like one.

The file becomes the interface:

```markdown
## Product
**Question:** What's the product? Name, one-liner, website.
**Used by:** every workflow
**Example answer:**
> Carnival — AI-native CRM that auto-fills pipeline data. usecarnival.com.

### Answer
<!-- agent writes here -->
```

The user copies `context.md.example` to `context.md`, opens it in Claude Code, says "help me get started," and Claude Code reads the questions, asks them conversationally, and writes answers into the `### Answer` blocks. Zero skill code, zero wizard, zero CLI.

Workflows then parse only the `### Answer` body and ignore the scaffolding (Question / Used by / Example). One regex change to `_section_body()` gets you there:

```python
section = re.search(rf"(?ms)^##\s+{re.escape(header)}\s*$\n(.*?)(?=^##\s+|\Z)", text).group(1)
ans = re.search(r"(?ms)^###\s+Answer\s*$\n(.*?)(?=^###\s+|\Z)", section)
body = ans.group(1) if ans else section  # fallback keeps legacy context files working
```

The fallback is critical — it makes the new shape backwards-compatible with any context file written before the `### Answer` convention existed.

Key insight: the existing tool (Claude Code) is the wizard. Stop building wizards on top of wizards.

### 20. CLAUDE.md is dual-audience: onboarding script + dev rules in one file

Most repos use `CLAUDE.md` purely for developer rules (folder structure, conventions). For an OSS GTM tool whose users are *non-technical*, `CLAUDE.md` does a second job: it's the **onboarding script** that tells Claude Code how to behave when a founder/marketer/sales lead opens the repo.

Structure:

```markdown
## When a user opens this repo
[onboarding instructions — greet, offer tour vs setup, walk through context.md,
 run a workflow, narrate progress in plain English]

### Language rules
- Never say: LLM, context window, Apify actor, venv, regex, CLI, repo
- Do say: Claude, the file we use to remember things, the scraper, first-time setup

## Developer notes (for forking / extending)
[folder structure, scraper rules, workflow rules — original dev content]
```

The user-facing section comes first because Claude Code reads top-to-bottom and the most likely visitor is a non-technical user, not a forker. The dev section gets a "When the user is technical, drop the hand-holding" escape hatch.

Why one file and not two: `CLAUDE.md` auto-loads. `CONTRIBUTING.md` doesn't. Splitting them means the onboarding instructions are also live for forkers (a feature, not a bug — they should still be able to demo the user flow), while dev rules stay readable for end users who scroll past.

### 21. Backwards-compatible parser changes: legacy fallback in the regex

When changing the shape of a config file that already has users, never make the new shape mandatory. Build the parser to look for the new shape first and fall back to the old shape if the new markers aren't there:

```python
ans = re.search(r"^###\s+Answer\s*$\n(.*?)", section)
body = ans.group(1) if ans else section  # legacy: read the section directly
```

One line. No migration script, no version flag, no breakage. Both shapes coexist in the same parser indefinitely. When you eventually drop legacy support (years later, if ever), you remove the fallback in one commit.

This applies to `_section_body()` for context.md, but the pattern generalizes: any time you're adding a new wrapper / sub-header / marker to an existing file format, the fallback line is the cheapest forward-compat insurance you'll ever write.

### 22. README tone for OSS launch: lowercase, opinionated, concrete

What works in a sea of generic GTM repos: a README that sounds like a founder talking, not a corporate landing page. Concrete rules:

- **Lowercase headings.** "what it does" beats "What It Does" beats "## Capabilities Overview." Signals casual / hacker, not enterprise.
- **One-line pitch up top, bolded thesis underneath.** "Your GTM team is a folder of python scripts" + "**this is the sales/marketing/content stack you actually own.**" The reader gets the angle in 10 seconds.
- **A table with concrete outputs** — not features. "what you get back" matters more than "what it does." "Google sheet of leads + drafted messages" beats "AI-powered lead generation."
- **Real numbers.** "$1–5 per workflow run" beats "cost-effective." Ranges with units.
- **A "why this exists" section that names the enemy.** Subscription SaaS that owns your data, $200/seat/month, can't take any of it with you. People remember the villain.
- **Banned phrases:** "leverage," "unlock," "synergy," "powered by AI," "in this fast-paced world," "thought leadership," "transform your," "supercharge."
- **No install commands or flag tables on the front page.** Those go in workflow READMEs. The front page is a sales page; depth lives one click away.

Reference for shape and voice: github.com/MitchellkellerLG/research-process-builder — same lowercase / table / bullet pattern, no jargon, opinionated tone.

---

## Workflow checklist (apply to every workflow before publishing)

- [ ] LLM column detection at start of `main()` for inputs AND outputs
- [ ] Field metadata in a single `*_FIELDS` list (key/label/desc)
- [ ] Both `GoogleSheetsBackend` and `CsvBackend` wired in
- [ ] `--sheet-id` ↔ `--input-csv` mutex; both flush after every write
- [ ] Pre-step that fills missing required inputs via web search
- [ ] Formatting rules in the extraction prompt (funding/HQ/year etc.)
- [ ] Expensive steps gated by classification (DMs only, P0 only by default)
- [ ] Step-spanning caches declared before Step 1
- [ ] Header writes use `headers[idx]`, never literal default names
- [ ] Stub-skip pattern around unbuilt scrapers/skills
- [ ] No `--project` arg; no project-specific references; no hard-coded sheet IDs / Drive folder IDs / company lists / trend queries baked into the workflow file
- [ ] If the workflow needs free-form context (project description, goals, preferences), use the **interactive context-backfill** pre-step (pattern #11) with `--auto` for CI
- [ ] `ensure_headers()` uses the schema-divergence guard (pattern #12) — never silently rewrites headers over existing data
- [ ] Named column-index constants (`COL_*`) defined next to `SHEET_HEADERS` so reorders are 1-line edits
- [ ] Long LLM-generated text fields (rationale / "why" columns) are constrained in the prompt to ~25 words / 2 lines — sheets are not paragraphs
- [ ] LLM prompts contain no hardcoded industry / persona / framework nouns (pattern #15) — every concrete noun comes from user context
- [ ] Every sheet-bound column has an explicit word/line cap, an explicit "insufficient data" sentinel for missing data, and (where filterable) a canonical enum list (pattern #16)
- [ ] If the workflow ships with built-in default configs (creator lists, query buckets, etc.), the pre-step uses pattern #17: prompt → on skip, **print the defaults** about to be used; never silently inherit author opinions
- [ ] Structured config in `context.md` uses pipe-delimited single-line records, not JSON (pattern #18) — humans should be able to add a creator without learning a schema
- [ ] `_section_body()` extracts from `### Answer` block first, falls back to section body for legacy context files (pattern #19, #21)
- [ ] If the workflow dumps the whole `context.md` into an LLM prompt, run it through `_strip_scaffolding()` first so Question/Example/Used-by lines don't pollute the prompt (pattern #19)
- [ ] No `/setup-context`-style helper skill or CLI wizard built — Claude Code reads `context.md.example` directly and runs the interview (pattern #19)
- [ ] README rewritten with: setup, quickstart (CSV + Sheets), step table, output formatting, CLI reference, cost estimate, known limitations
- [ ] `python -m workflows.X.workflow --help` runs cleanly
- [ ] End-to-end smoke test on a real sheet/CSV with messy column names
