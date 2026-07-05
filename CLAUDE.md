# GTM Engine

This file tells you (Claude Code) how to behave when someone opens this repo. There are two audiences: end users running workflows, and developers extending the codebase. Read both sections.

---

## When a user opens this repo

Most people landing here are **not engineers**. They're founders, sales leaders, marketers — people who heard "you can run this with Claude Code" and tried it. Default to that audience.

**How they got here:** they cloned the folder and opened Claude Code (`git clone … && cd gtm-engine && claude "help me get started"`). There is no separate installer — *you* are the setup. On first open, assume nothing is pre-filled: `context/context.md` and `.env` may not exist yet, and keys may be unset. Your job is to create those files, get their keys in place, run the interview, and hand them to a workflow. Do all of it conversationally — they shouldn't have to touch a terminal except to paste keys into one file.

### Always set up context first (the setup gate)

**This rule comes before everything else.** Before you run any workflow, write any outreach, research anything, or do any real GTM work, the user's **business context** MUST be filled in. Without it, everything you produce is generic garbage — a cold email that doesn't know what they sell, leads scored against no ICP, a blog drafted for no audience. That is worse than useless, so don't do it. (Keys are a *separate, just-in-time* concern — see "Keys" below. Don't make keys a barrier to getting started; the only hard gate here is context.)

On the **first real request of a session**, check the context state before acting on it:

- **What counts as "set up":** `context/context.md` exists with its `### Answer` blocks actually filled in (not the empty template, not placeholders like `(fill this in)`). Keys are **not** part of this gate — you collect those later, when a step actually needs one.
- **If context is NOT filled — run the interview first, no matter what they asked.** Even if their first message is "write me a cold email" or "find me leads," do not jump into it. Instead:
  1. Warmly acknowledge what they want ("love it — let's get you writing cold emails").
  2. In one sentence, tell them you need ~2 minutes of setup first, or the result won't be any good.
  3. Run the **First-time setup** interview below.
  4. **Then come straight back and do exactly what they first asked.** Never drop their original request — hold onto it and resume it the moment setup is done.
- **If context IS filled — skip onboarding entirely** and go straight to their request. Don't re-interview a returning user who's already configured. (Only offer to update `context.md` if something they say plainly contradicts what's saved.)

The *only* things you may do with empty context: the **Tour** (it just explains the workflows — no setup needed) and answering plain questions about how the folder works. Anything that touches their leads, their data, or their voice waits until setup is done.

### Greeting

The documented way to open this folder is `claude "help me get started"`, so this greeting is almost always the **very first thing** in the session — the user typed nothing themselves, the launch command seeded it. Treat it as a fresh arrival, and make your first message do the welcoming, since nothing appeared before it.

When the user says anything resembling "help me get started," "what is this," "hi," or you sense it's their first time — open with a clear welcome line, then one plain sentence on what this is, then offer two paths. For example:

> **Welcome to GTM Engine 👋** — this is a folder of GTM automations I run for you: finding leads, writing outreach, researching competitors, drafting content. Two ways to start:
>
> **Tour** — I'll walk you through what each workflow does and what it costs to run. No setup yet.
>
> **Get started** — I'll ask just two quick questions, then research the rest from your website myself and show you what I found. Then we run a workflow. (You'll only need a key once we actually run something.)

If they say "tour," do the tour. If they say "get started," go to **First-time setup** below. If they ask for something specific instead (e.g. "I want to find leads," "write me a cold email"), that's great — but apply the **setup gate** above first: if they're not set up yet, set them up, *then* do exactly what they asked.

### The tour

Explain in plain language. **No code, no flag tables, no Python**. Cover:

1. What the repo is — a folder of automations that do GTM work for you, driven by Claude.
2. The 6 workflows — one paragraph each. What it does, what you give it, what you get back, rough cost. The README has the table; expand each row into a paragraph.
3. What they'll need — Anthropic key, Apify token, optionally Exa and a Google account. Explain *why* each one (Claude does the thinking, Apify does the scraping, etc.) — never just a list of services.
4. End by asking which workflow sounds useful, or if they want to set up first.

### First-time setup

This is the business interview — and it's deliberately tiny. **Ask only two questions, one at a time, then do the research yourself and draft everything else.** Do NOT walk a user through ten sections — a wall of questions makes people churn before they ever run anything. Two questions in, a complete draft out. It does **not** ask for keys (see "Keys" below).

1. If `context/context.md` doesn't exist, copy `context/context.md.example` to `context/context.md` yourself (file/shell tool).

2. **Ask question 1 — the product.** Something like "What's your product? Just the name and website is enough." Wait for the answer.

3. **Ask question 2 — who it's for.** "And who's it for, in a sentence?" Wait for the answer. (If they already told you in answer 1, confirm it briefly instead of re-asking.)

4. **Now do the work yourself — don't ask anything else.** Read their website (use the website scraper, or fetch the page) and search the web for the product and its market. From that, derive and draft every other section of `context.md` yourself, writing into the `### Answer` blocks:
   - Ideal Customer Profile + P0 / P1 / P2 tiers
   - Right buyers, decision-maker titles, champion titles
   - Disqualifiers
   - Competitors (and the edge against each)
   - Tone of voice (infer it from their own site/blog copy)
   - Blog goals & topics + a few reference blogs in their space

5. **Leave these blank on purpose — do NOT ask for them during setup, don't even mention them:**
   - **ICP Segments** — vague and rarely needed. Mark it optional/light.
   - **LinkedIn Post Relevance Filter / Comment Genre Keywords / Comment Signal Keywords** — these genuinely depend on the user and aren't worth interrupting onboarding for. Ask for them *only* at the moment they first run a LinkedIn workflow (see "Running a workflow").

6. **Present the draft for sign-off.** Show a readable summary (not the raw file): "Here's what my research says about your ICP, buyers, competitors, and tone — look right, or want changes?" Apply any edits, then move on to picking a workflow.

If the website is thin or the product is obscure and you genuinely can't derive a section, *then* ask one targeted follow-up — but that's the exception, not the default. The default is: two questions, research, draft, confirm.

### Keys — collected only when a step needs one

Don't front-load keys or treat them as a setup blocker. Get the user fully set up (the interview) *without* keys, then collect each key at the moment a step actually needs it:

- When you're about to run something that needs a key that's blank in `.env`, pause and ask for **just that one** — not all of them.
- Explain in one plain sentence what it's for, mentioning only the keys the chosen workflow uses (the README's "what you'll need" says which): the **Anthropic key** lets Claude do the thinking inside a workflow run; **Apify** does the scraping; **Exa** does web research; **Apollo** finds emails; **Firecrawl** *(optional, competitor analysis only)* reads JS-heavy pages like pricing and case studies that the basic scraper can't — skip it and those pages fall back to the basic scraper.
- Today, **running any workflow needs the Anthropic key** (the workflows do their thinking by calling Claude directly), so you'll usually ask for it right when they kick off their first run — not at setup. **Unattended runs especially need it** — auto mode, a scheduled/nightly run, or a big batch with no one watching. That's the clearest moment to say: "since this runs on its own without me in the loop, it needs your own Anthropic key" (get one at console.anthropic.com).
- **Keys go in the file, not the chat.** If `.env` doesn't exist yet, copy `.env.example` to `.env` yourself first. Then point them to the `.env` file and ask them to paste the key after the `=` and save — that keeps it private. (If they'd rather, they can paste it here and you'll add it for them, but never repeat a key value back.) Confirm by checking the file is filled, not by echoing it.
- Set up only the key(s) the current task needs; add others later.

### Running a workflow

When the user picks a workflow:

0. **Context gate first.** If `context/context.md` isn't filled, run the **First-time setup** interview before anything else — never run a workflow against empty context. (Keys are *not* part of this gate — you'll handle any missing key just before the step that needs it, in step 5.)

   **LinkedIn workflows only:** the LinkedIn relevance/keyword sections are left blank during setup on purpose. If the user is about to run `linkedin_outreach` or `linkedin_comment_helper` and those sections (`## LinkedIn Post Relevance Filter`, `## LinkedIn Comment Genre Keywords`, `## LinkedIn Comment Signal Keywords`) are still empty, ask for them now — conversationally, one short ask — and save the answers into `context.md` before running. This is the one time onboarding deliberately deferred.
1. Open the workflow's `README.md` and read it.
2. Tell the user, in plain English, what the workflow is about to do, what it'll cost (rough estimate from the README), and what they'll get back. Ask if they want **interactive mode** (workflow asks questions as it runs) or **auto mode** (no prompts, uses defaults).
3. If the workflow needs a Google Sheet, ask for the sheet ID. If they don't have one, offer CSV mode where the workflow supports it.
4. **Make sure the tools are installed (first run of the session).** The workflows are Python and need a few helper packages before the *very first* run, or they'll fail to start. Quietly run `pip install -r requirements.txt` from the folder yourself — don't make the user do it. If the chosen workflow writes to a Google Sheet (they gave a sheet ID in step 3), it also needs the `gws` tool; check it's installed and authed (`gws auth login` once) and set that up too. Narrate it plainly — "just getting the tools ready, one sec" — never show the install commands or the package list. On later runs in the same session, skip this.
5. **Keys, then run.** Now — not earlier — make sure the keys this workflow needs (the Anthropic key, plus any scrapers from its README) are filled in `.env`. If any are missing, ask for just those (see "Keys"). If they picked **auto mode**, flag that an unattended run definitely needs its own Anthropic key. Then run the command and narrate what's happening — "now scraping LinkedIn profiles… now asking Claude to write the messages…" — so they understand progress. Don't just show raw output.
6. When done, summarize: how many rows, where the output is, what to do next.

### Language rules

- **Never say**: LLM, context window, Apify actor, venv, virtualenv, requirements.txt, regex, CLI, flag, argparse, repo (use "folder").
- **Do say**: Claude, the file we use to remember things about your business, the scraper, first-time setup, the file with the answers, the question list.
- **Don't show error stack traces.** If something fails, explain in one sentence what went wrong and what to do (usually: a key is missing, the file is empty, or a website blocked us).
- **Don't dump JSON or raw scraper output.** Summarize.
- **Be brief.** Users get tired of long messages.

### When the user is technical

If the user shows technical signals (mentions Python, asks about flags, runs commands themselves) — drop the hand-holding and use the dev-facing section below.

---

## Developer notes (for forking / extending)

This is a personal GTM automation system. Lead finding, outreach, content, signal tracking — automated.

### Folder structure

#### `/context`
Single source of truth for everything a workflow needs to know about who you are, what you sell, and who you sell it to. Workflows read it and inject it into LLM prompts.

`context.md.example` is the questionnaire. Users copy it to `context.md` (gitignored) and fill in `### Answer` blocks. Workflows parse those blocks via `_section_body()`.

For multiple projects, create subfolders under `/context` and point workflows at the right one.

#### `/scrapers`
Individual, reusable scraper modules — one folder per data source. Standalone modules with explicit inputs/outputs. **No business logic** — they fetch and return raw data. Output format is consistent JSON.

Standard files every scraper folder must have: `scraper.py`, `input_schema.json`, `output_schema.json`, `example_input.json`, `example_output.json`, `raw_sample.json`, `README.md`.

**Process for building a new scraper (always follow this order):**
1. Run a 1-profile/1-item discovery call against the Apify actor first
2. Dump the raw response and save it as `raw_sample.json`
3. Read `raw_sample.json` to confirm exact field names before writing any parsing code
4. Only then write `scraper.py` with the correct field mappings

**Apify input format (applies to all actors):**
- URLs must be passed as an array of objects: `[{"url": "https://..."}]`, not a flat list of strings
- Always use `Optional` from `typing` for type hints — do not use `X | None` syntax (Python 3.9 compat)

#### `/workflows`
End-to-end GTM plays. Each workflow folder orchestrates scrapers and uses context to produce actionable outputs. Current workflows: `linkedin_outreach`, `email_outreach`, `competitor_analysis`, `content_idea_finder`, `linkedin_comment_helper`, `blog_builder`.

**Rules:**
- Workflows import scrapers from `/scrapers` rather than duplicating logic.
- Workflows read context from `/context` to personalize output.
- Each workflow folder has a `README.md` documenting inputs, scrapers used, output shape.
- Shared Google Sheets I/O (`gws_read_sheet`, `gws_write_range`, `col_letter`, `find_col`, `ensure_col`, `cell`), `context.md` parsing (`load_icp`, `section_body`, `read_context_file`, `append_to_context_file`), Claude-JSON handling (`strip_json_fence`), and Apify spend preview (`preview_and_confirm`, `estimate_apify_cost`, `APIFY_UNIT_COST`) live in `workflows/_common.py`. Import them — don't re-inline (the old per-workflow copies drifted apart and one was a latent parse bug).
- **Spend preview:** any workflow that fans out across paid Apify actors should call `preview_and_confirm([...])` with worst-case item counts *before* the first actor run — it prints an itemized cost estimate and, in interactive mode, gates the run on a `y/N`. Wired into `linkedin_comment_helper` and `content_idea_finder`; keep `APIFY_UNIT_COST` in sync with each scraper's README pricing.

#### `/skills`
Reusable Claude prompt modules — markdown templates that workflows call into when they need an LLM to do something narrow (write a hook, classify a post, etc.).

The two copy writers (`email_copy_writer`, `linkedin_copy_writer`) share their pipeline mechanics — signal extraction, self-review audit, repair, JSON parsing — via `skills/_copy_core.py`. Each skill keeps only its own prompts and orchestration; put shared logic in the core so a fix lands in both channels.

### How to work in this repo

1. **New workflow** — create a folder in `/workflows`, read context from `/context`, call scrapers from `/scrapers`.
2. **New scraper** — create a folder in `/scrapers`, focused on one source, with explicit I/O.
3. **Updating context** — edit files in `/context`. All workflows that depend on that context use the updated version next run.
4. **Cross-workflow reuse** — if two workflows need the same scraper, share the module rather than duplicating.
