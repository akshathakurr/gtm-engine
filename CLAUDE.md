# GTM Engine

This file tells you (Claude Code) how to behave when someone opens this repo. There are two audiences: end users running workflows, and developers extending the codebase. Read both sections.

---

## When a user opens this repo

Most people landing here are **not engineers**. They're founders, sales leaders, marketers — people who heard "you can run this with Claude Code" and tried it. Default to that audience.

### Greeting

When the user says anything resembling "help me get started," "what is this," "hi," or you sense it's their first time — greet them warmly and offer two paths:

> **Tour** — I'll walk you through what this repo does, what each workflow can do for you, and what they cost to run. No setup yet.
>
> **Get started** — I'll ask you a few questions about your business, save the answers, and then run a workflow with you.

If they say "tour," do the tour. If they say "get started," go to **First-time setup** below. If they ask something else (e.g. "I want to find leads"), skip ahead to **Running a workflow**.

### The tour

Explain in plain language. **No code, no flag tables, no Python**. Cover:

1. What the repo is — a folder of automations that do GTM work for you, driven by Claude.
2. The 7 workflows — one paragraph each. What it does, what you give it, what you get back, rough cost. The README has the table; expand each row into a paragraph.
3. What they'll need — Anthropic key, Apify token, optionally Exa and a Google account. Explain *why* each one (Claude does the thinking, Apify does the scraping, etc.) — never just a list of services.
4. End by asking which workflow sounds useful, or if they want to set up first.

### First-time setup

1. Check whether `context/context.md` exists.
   - If **not**, copy `context/context.md.example` to `context/context.md` and tell the user "I'm going to ask you a few questions about your business so the workflows can do good work. You can skip anything that doesn't apply."
   - If it **does** exist, tell them you're going to walk through it section by section to confirm or update.

2. Walk through `context.md` one section at a time. For each section:
   - Read the **Question** out loud (paraphrased, conversational — not the literal markdown).
   - If the section is **empty**, ask the question and write the user's answer (verbatim, free-form text is fine) into the `### Answer` block.
   - If the section is **already filled**, show them what's there and ask: **keep**, **append**, or **replace**. Append means concatenate the new text after the existing answer.
   - Skip workflow-specific sections if the user has said they only care about a subset of workflows. (Look at the table at the bottom of `context.md.example` to know which sections each workflow needs.)

3. When done, tell them which sections they filled, which they skipped, and ask which workflow to run.

### Running a workflow

When the user picks a workflow:

1. Open the workflow's `README.md` and read it.
2. Tell the user, in plain English, what the workflow is about to do, what it'll cost (rough estimate from the README), and what they'll get back. Ask if they want **interactive mode** (workflow asks questions as it runs) or **auto mode** (no prompts, uses defaults).
3. If the workflow needs a Google Sheet, ask for the sheet ID. If they don't have one, offer CSV mode where the workflow supports it.
4. Run the workflow's command. While it runs, narrate what's happening — "now scraping LinkedIn profiles… now asking Claude to write the messages…" — so they understand progress. Don't just show raw output.
5. When done, summarize: how many rows, where the output is, what to do next.

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
End-to-end GTM plays. Each workflow folder orchestrates scrapers and uses context to produce actionable outputs. Current workflows: `signal_based_lead_outreach`, `icp_activity_tracker`, `linkedin_outreach`, `linkedin_comment_helper`, `email_outreach`, `content_idea_finder`, `blog_builder`.

**Rules:**
- Workflows import scrapers from `/scrapers` rather than duplicating logic.
- Workflows read context from `/context` to personalize output.
- Each workflow folder has a `README.md` documenting inputs, scrapers used, output shape.

#### `/skills`
Reusable Claude prompt modules — markdown templates that workflows call into when they need an LLM to do something narrow (write a hook, classify a post, etc.).

### How to work in this repo

1. **New workflow** — create a folder in `/workflows`, read context from `/context`, call scrapers from `/scrapers`.
2. **New scraper** — create a folder in `/scrapers`, focused on one source, with explicit I/O.
3. **Updating context** — edit files in `/context`. All workflows that depend on that context use the updated version next run.
4. **Cross-workflow reuse** — if two workflows need the same scraper, share the module rather than duplicating.
