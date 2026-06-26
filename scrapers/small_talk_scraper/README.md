# Small Talk Scraper

Finds **humanizing, conversational** details about a prospect — the kind of thing
you'd mention casually to make cold outbound feel like a real human wrote it.

Optimizes for:
> "Would this help write a genuinely human opening line?"

NOT:
> "Would this help enrich a CRM?"

## Good vs bad output

| Good (keep) | Bad (reject) |
|---|---|
| Arsenal fan | Raised funding |
| Marathon runner | CEO at company |
| Plays Valorant | Hiring engineers |
| Coffee nerd | Passionate about AI |
| Tweets through F1 races | Building the future |
| DJed in high school | Startup operator |

## Pipeline

```
1. Identity resolution
   - Use given LinkedIn / Twitter URLs.
   - If no Twitter URL: 1 Exa search + Claude verification (cross-references company).

2. Signal harvest
   - Pull recent tweets (replies > posts for authenticity).
   - Claude generates 5 targeted Exa queries across human-signal categories
     (hobbies, gaming, fandoms, lifestyle, quirks). Run in parallel.

3. Extraction (with strict identity verification)
   - Claude pulls humanizing signals from tweets + web results.
   - Hard-excludes business / funding / job content.
   - **Identity gate**: every signal must anchor to one of —
       (a) source explicitly mentions the target company, OR
       (b) source references the LinkedIn handle, OR
       (c) source has a unique biographical detail consistent with the LinkedIn.
     Anything weaker is rejected. A wrong-person signal is worse than no signal.
   - Emits {topic, evidence_quote, source_url, source_type, identity_anchor,
     confidence, small_talk_score}.

4. Scoring & dedup & selection
   - Claude scores small-talk-worthiness 0-10.
   - **Fact-level dedup** (not just topic-label dedup): if signal A's quote
     already contains signal B's fact, they collapse into one richer signal.
     E.g. "I wrestled for Penn State and did cage fighting" is ONE signal
     ("Combat sports — Penn State wrestler + cage fighter"), not two.
   - Unique source URL per signal. Quotes capped at 100 chars.
   - Top 2-3 win. If nothing genuinely humanizing exists, returns empty.

5. Output
   - small_talk: 2-3 line string with source — directly consumable by the
                 email_outreach / linkedin_outreach workflows.
   - signals:    structured list (for downstream skills like personalisation_hook).
   - identity:   the resolved identity graph.
```

## Inputs

| Field | Required | Notes |
|---|---|---|
| `name` | yes | Full name |
| `company` | recommended | Disambiguates Twitter handle |
| `profile_url` | optional | LinkedIn URL (workflow's primary handle) |
| `twitter_url` | optional | Skip identity resolution if provided |
| `website` | optional | Personal site |
| `max_signals` | default 3 | Cap on signals returned |
| `max_tweets` | default 60 | Tweet pull size |
| `days_back` | default 180 | Twitter time window |
| `num_queries` | default 5 | Targeted web searches Claude generates |
| `skip_twitter` | default false | Skip Apify cost — uses web search only |

## Output

```json
{
  "small_talk": "- F1 fan, Ferrari camp — \"Strategy disasterclass again 😭\" [twitter: ...]\n- Coffee nerd ...\n- Anime watcher ...",
  "signals":  [{"topic": "...", "evidence_quote": "...", "source_url": "...", ...}, ...],
  "identity": {"name": "...", "twitter_url": "...", "twitter_confidence": 0.92, ...}
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

- `EXA_API_KEY` — Exa-backed web search
- `APIFY_API_TOKEN` — Twitter scraping (set `skip_twitter=true` to avoid)
- `ANTHROPIC_API_KEY` — query generation, identity verification, signal extraction & scoring

## Cost (rough, per prospect)

- Anthropic: ~3-4 calls (queries + identity + extract). ~$0.01-0.03 with Sonnet 4.6.
- Exa: ~5 search calls (~$0.025).
- Apify (Twitter): ~$0.005 per profile (20-tweet minimum), **only if a Twitter handle is found**.

Total: **~$0.04-0.06 per prospect** with Twitter, **~$0.04 without**.

## Design principles

1. Optimize for HUMANITY, not enrichment.
2. Specificity > broadness. Weird quirks are gold.
3. Replies/comments > posts.
4. Multiple weak signals can combine into a strong inference (Claude does this).
5. If nothing genuinely humanizing exists, return empty — never pad with business stuff.
