# Small Talk Scraper

Finds **humanizing, conversational** details about a prospect — the kind of thing
you'd mention casually to make cold outbound feel like a real human wrote it.

Optimizes for:
> "Would this help write a genuinely human opening line?"

NOT:
> "Would this help enrich a CRM?"

**Web search only.** Twitter/X scraping was removed on purpose — it added a paid
Apify pull plus an identity-resolution search per lead for a source that's often
empty or the wrong person. The prospect's LinkedIn activity already reaches the
copy step via the LinkedIn posts scraper, so this scraper leans on precise,
source-targeted web search instead.

## Good vs bad output

| Good (keep) | Bad (reject) |
|---|---|
| Arsenal fan | Raised funding |
| Marathon runner | CEO at company |
| Plays Valorant | Hiring engineers |
| Coffee nerd | Passionate about AI |
| Talks F1 constantly | Building the future |
| DJed in high school | Startup operator |

## Pipeline

```
1. Query generation
   - Claude writes N precise, source-targeted queries across human-signal
     categories (hobbies, gaming, fandoms, lifestyle, quirks), explicitly
     aimed at podcasts / interviews / personal blogs / Reddit / YouTube.
   - Falls back to templated precise queries if the LLM call fails.

2. Signal harvest
   - Run those web searches (via search_web_batch — Exa or Parallel).

3. Extraction (with strict identity verification)
   - Claude pulls humanizing signals from the web results.
   - Hard-excludes business / funding / job content.
   - The static instruction prefix is prompt-cached, so a run over many leads
     pays for it once; only the per-lead target + corpus are uncached.
   - **Identity gate**: every signal must anchor to one of —
       (a) source explicitly mentions the target company, OR
       (b) source references the LinkedIn URL, OR
       (c) source has a unique biographical detail consistent with the LinkedIn.
     Anything weaker is rejected. A wrong-person signal is worse than no signal.
   - Emits {topic, evidence_quote, source_url, source_type, identity_anchor,
     confidence, small_talk_score}.

4. Scoring & dedup & selection
   - Claude scores small-talk-worthiness 0-10.
   - Fact-level dedup + unique source URL per signal. Quotes capped at 100 chars.
   - Top 2-3 win. If nothing genuinely humanizing exists, returns empty.

5. Output
   - small_talk: 2-3 line string with source — directly consumable by the
                 email_outreach / linkedin_outreach workflows.
   - signals:    structured list (for downstream skills like personalisation_hook).
   - identity:   the identity anchors used (name, company, LinkedIn, website).
```

## Inputs

| Field | Required | Notes |
|---|---|---|
| `name` | yes | Full name |
| `company` | recommended | Disambiguates same-name people during verification |
| `profile_url` | optional | LinkedIn URL — used only as an identity anchor, not scraped |
| `website` | optional | Personal site (identity anchor) |
| `max_signals` | default 3 | Cap on signals returned |
| `num_queries` | default 3 | Targeted web searches Claude generates |

## Output

```json
{
  "small_talk": "- F1 fan — \"...\" [podcast: ...]\n- Coffee nerd ...\n- Anime watcher ...",
  "signals":  [{"topic": "...", "evidence_quote": "...", "source_url": "...", ...}, ...],
  "identity": {"name": "...", "company": "...", "linkedin_url": "...", "website": "..."}
}
```

The `small_talk` string is what the workflow stores in the **Small Talk** column.

## Usage

From a workflow:

```python
from scrapers.small_talk_scraper import scraper as small_talk

result = small_talk.scrape_small_talk(
    profile_url=lead["linkedin"],
    name=lead["name"],
    company=lead["company"],
)
print(result["small_talk"])
```

Standalone:

```bash
python -m scrapers.small_talk_scraper.scraper example_input.json
```

## Dependencies

- `EXA_API_KEY` and/or `PARALLEL_API_KEY` — web search (either works; Exa primary, Parallel fallback)
- `ANTHROPIC_API_KEY` — query generation + signal extraction & scoring

## Cost (rough, per prospect)

- Anthropic: 2 calls (queries + extract). ~$0.01-0.02 with Sonnet-class models.
- Web search: `num_queries` (default 3) calls × a few results each (~$0.015).

Total: **~$0.02-0.04 per prospect** — no Apify cost (Twitter removed).

## Design principles

1. Optimize for HUMANITY, not enrichment.
2. Specificity > broadness. Weird quirks are gold.
3. Verify identity hard — a wrong-person signal is worse than none.
4. Multiple weak signals can combine into a strong inference (Claude does this).
5. If nothing genuinely humanizing exists, return empty — never pad with business stuff.
