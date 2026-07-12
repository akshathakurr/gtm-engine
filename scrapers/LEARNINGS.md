# Scraper Learnings

Hard-won lessons from building scrapers in this repo. Read this before starting a new scraper — it will save significant time and money.

---

## Scraper code structure — canonical template

Every new scraper must follow this pattern exactly. Do not copy from an older scraper without checking it matches this template — earlier scrapers were built before this was standardised.

### 1. Module-level docstring (required)
Always open with a docstring covering: what the scraper does, actor name + pricing, input/output summary.
```python
"""
<Scraper name> — one-line description.

Actor: username/actor-name
Cost: $X.XX/result (pricing model). For N results = ~$X.XX.
No account or cookies required. / Requires Apify account.

Input:  param1, param2, ...
Output: list of X with field1, field2, ...
"""
```

### 2. Imports and token loading (required order)
```python
import os
import sys
import json
import time
import contextlib
import io
from typing import Optional, List   # ALWAYS use typing.List/Optional — NOT list[str] (breaks Python 3.9)

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "../../.env"))  # always load at module level
APIFY_TOKEN = os.getenv("APIFY_API_TOKEN")                          # read once at module level
```

- `load_dotenv` must be at module level, not inside a function. This is how Comment Scraper and Post Research do it. Earlier scrapers (Profile, Profile Post, Job) skip this — they work only if the env var is already exported in the shell.
- Read the token once at module level as `APIFY_TOKEN`. Don't re-read it inside every function call.

### 3. Log suppression (use this exact implementation)
```python
@contextlib.contextmanager
def _suppress_apify_logs():
    with contextlib.redirect_stderr(io.StringIO()):
        yield
```
This is the cleanest version. Do NOT use the devnull/stdout redirect pattern from older scrapers — it swallows print() output from the scraper itself.

### 4. Apify calls — use the SDK, not raw requests
The `ApifyClient` SDK handles polling automatically. Use it unless the actor requires a non-standard run flow.
```python
from apify_client import ApifyClient
from scrapers._apify import dataset_items, ApifyRunError

client = ApifyClient(APIFY_TOKEN)
with _suppress_apify_logs():
    run = client.actor(ACTOR_ID).call(run_input=payload)
items = dataset_items(client, run)   # reads run.default_dataset_id; raises ApifyRunError if run is None
```
Read the dataset via `dataset_items(client, run)` — never `run["defaultDatasetId"]`.
In **apify-client 3.x**, `.call()` returns a `Run` model (attribute access: `run.default_dataset_id`),
which is **not** subscriptable, so `run["defaultDatasetId"]` raises `TypeError`. It also returns
`None` when the run fails/aborts/times out; `dataset_items` guards that and raises `ApifyRunError`,
which a scraper inside a `try/except` turns into its normal error shape (never a stack trace to the user).
`requirements.txt` pins `apify-client>=3,<4` so this contract can't drift back to the 1.x dict form.

Do NOT use raw `requests.post` + manual polling (`_run_actor`, `_wait_for_run`, `_fetch_dataset`). The Post Research scraper does this — it works but it's more code for no benefit. SDK is the standard. (That scraper's `run` is a raw REST dict, so it alone still uses `run["defaultDatasetId"]`.)

### 5. Main function — always returns a dict, never raises
```python
def scrape_X(param1: str, param2: int = 20) -> dict:
    errors: List[str] = []
    results: List[dict] = []

    try:
        # ... call actor, parse results ...
    except Exception as e:
        errors.append(str(e))

    return {
        "param1": param1,
        "results": results,
        "result_count": len(results),
        "errors": errors,
    }
```
- Always `try/except` the entire actor call block. Network failures, actor errors, and bad responses must be caught and put in `errors[]`, not raised.
- Always return a consistent dict even on failure — callers should not need to handle exceptions.

### 6. Parsing — separate _parse_X() from fetching
```python
def _parse_item(raw: dict) -> dict:
    nested = raw.get("nestedField") or {}   # always use `or {}` not just `.get()` for nested dicts
    return {
        "field": raw.get("rawField", ""),   # always provide defaults
        "nested_field": nested.get("key", ""),
    }
```
- One `_parse_X()` function per result type. Never do field mapping inline in the main function.
- Always use `raw.get("key") or {}` pattern for nested dicts — not just `raw.get("key", {})` because the field might be present but `None`.

### 7. Deduplication — always do it
```python
seen = set()
for item in raw_items:
    uid = item.get("id", "")
    if uid and uid not in seen:
        seen.add(uid)
        results.append(_parse_item(item))
        if len(results) >= max_results:
            break
```
Actors frequently return duplicates, especially when paginating. Always deduplicate by a unique ID field.

### 8. CLI entry point — always present
```python
if __name__ == "__main__":
    input_file = sys.argv[1] if len(sys.argv) > 1 else "example_input.json"
    with open(input_file) as f:
        inp = json.load(f)

    result = scrape_X(
        param1=inp["param1"],
        param2=inp.get("param2", 20),
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
```

### What the older scrapers do differently (do not copy these patterns)
| Scraper | Deviation |
|---|---|
| LinkedIn Profile Scraper | No `load_dotenv`. Uses `list[str]` type hint (Python 3.10+ only). Uses `logging.getLogger` for log suppression instead of context manager. |
| LinkedIn Profile Post Scraper | No `load_dotenv`. Devnull stdout/stderr redirect in `_suppress_apify_logs`. |
| LinkedIn Job Scraper | No `load_dotenv`. Devnull stdout/stderr redirect in `_suppress_apify_logs`. |
| LinkedIn Post Comment Scraper | Uses `load_dotenv` ✓. Uses SDK ✓. But uses devnull redirect instead of `redirect_stderr`. |
| LinkedIn Post Research Scraper | Uses `load_dotenv` ✓. But uses raw requests instead of SDK — unnecessary extra code. |

---

## Process (always follow this order)

1. **Find the actor** — check the Apify store. For LinkedIn prefer `harvestapi` (primary since 2026-07-09 — $2/1k posts, $4/1k profiles, one shared item shape parsed by `scrapers/_harvest.py`) with `apimaestro` as the known-good fallback. Check pricing model before running anything.
2. **Read docs via the Apify REST API** — Apify store pages are JS-rendered and useless to fetch. Use the API directly:
   ```bash
   curl "https://api.apify.com/v2/acts/{username}~{actor-name}?token=$APIFY_API_TOKEN"
   ```
3. **Run a 1-item discovery call** — never skip this. Dump the response to `raw_sample.json`.
4. **Read `raw_sample.json`** — confirm exact field names before writing any parsing code.
5. **Write `scraper.py`** — only now, using confirmed field names from step 4.

Skipping the discovery call means your field names are guesses. This has already caused one scraper (LinkedIn Company Post Scraper) to ship with unverified field mappings.

---

## Apify actor rules

### Picking an actor
- **Prefer `harvestapi` for LinkedIn** (since 2026-07-09) — 60% cheaper posts, server-side `maxPosts`/`postedLimitDate` (no pagination loops), one shared output shape across its post actors. `apimaestro` is the tested fallback: consistent, API-based, but $5/1k posts.
- **Avoid browser crawlers** — `supreme_coder/linkedin-post` looked cheap at $1/1k posts but was a browser crawler that made 280+ requests for 3 posts and cost $1.51 for one aborted run. Always check the pricing model type: `per result` is safe, `compute units` or `browser sessions` is risky.
- **Check user reviews** on the store page before committing to a new actor.

### Input format (applies to all actors)
- URLs must be passed as an array of objects: `[{"url": "https://..."}]`, not a flat list of strings.
- Always use `Optional` from `typing` for type hints. Do NOT use `X | None` syntax — the system runs Python 3.9, which doesn't support it.

### Known actor quirks
| Actor | Quirk |
|---|---|
| *(fallback-era, apimaestro)* `apimaestro/linkedin-profile-posts` | Must explicitly pass `username` extracted from URL. If omitted, actor silently defaults to `satyanadella` for every run. |
| `apimaestro/linkedin-profile-posts` | Pagination token is on the **last item** of each page, not in response metadata. |
| `apimaestro/linkedin-profile-posts` | No built-in delay between profile calls. When scraping 50+ profiles in a loop, add `time.sleep(5)` between calls in the workflow — LinkedIn can throttle the actor session on bursts. |
| All `apimaestro` actors | Stream verbose logs to stderr — suppress with the `_suppress_apify_logs()` context manager (see any built scraper). |

### Rejected actors
| Actor | Reason |
|---|---|
| `supreme_coder/linkedin-post` | Browser crawler. Cost $1.51 for 1 aborted run on 3 posts. Do not use. |
| `harvestapi/linkedin-profile-posts` | Input format unclear, returned 0 results on test run. |
| `apidojo/twitter-profile-scraper` | Blocks free Apify plan entirely — returns demo data with `{"demo": true}`. Do not use on free plan. |
| `xtdata/twitter-x-scraper` | Ignores `maxTweetsPerHandle: 5`, returned 718 tweets and cost $2.93. Do not use. |

### Actor default traps
- `apimaestro/linkedin-company-posts` defaults to **Google** posts if the input format is wrong. If you see Google results when you didn't ask for them, the input format is wrong — not a slug issue.
- `apimaestro/linkedin-profile-posts` defaults to **satyanadella** if `username` is omitted.
- Always verify `source_company` / `author` in the raw result to confirm you got the right company.

### curious_coder actors use flat URL strings, not objects
`curious_coder/linkedin-jobs-scraper` expects `urls` as a flat array of strings: `["https://..."]`.
NOT `[{"url": "https://..."}]`. The `{url: ...}` format throws `Invalid URL` — it tries to use the whole object as a URL string.
This is the opposite of all `apimaestro` actors. Always check which provider the actor belongs to.

### maxResults is often ignored
`curious_coder/linkedin-jobs-scraper` ignores `maxResults: 1`. It fetches a full page (~25 jobs minimum) regardless. Always cap results client-side with `items[:max_jobs]` after fetching.

### Never guess company slugs
LinkedIn company slugs (the identifier in the URL) are not always derivable from the company name — e.g. "2am VC" → `2-a-m-ventures`. Always require a full LinkedIn company URL as input. Never try slug variations in live calls to find the right one — that burns credits for nothing.

---

## Code patterns (copy from existing scrapers, don't reinvent)

### Log suppression — must be thread-safe
Disable Apify actor-run log streaming at the source by passing `logger=None` to
`.call(...)`, and silence the client logger once at import. Do **NOT** swap
`sys.stdout`/`sys.stderr` (or use `contextlib.redirect_stderr`) — a global stream
swap corrupts output when scrapers run concurrently (workflows now parallelize
scrapes across worker threads), leaving stdout pointed at a closed/devnull stream.

```python
import logging
from contextlib import contextmanager

# Once at import — thread-safe, happens before any concurrent use.
logging.getLogger("apify_client").setLevel(logging.WARNING)

@contextmanager
def _suppress_apify_logs():
    # No-op kept for call-site compatibility; streaming is disabled per call below.
    yield

# At every call site:
run = client.actor(ACTOR_ID).call(run_input=payload, logger=None)
items = dataset_items(client, run)
```
The old stdout/stderr-swapping version is unsafe under concurrency — every scraper
that swaps streams in one thread corrupts *all* threads' output for that window.

### Deduplication — always track seen URNs
Apify actors can return the same item across paginated calls. Always maintain a `seen_urns` set and skip duplicates.

### Cap results immediately
Always do `all_raw = all_raw[:max_posts]` after the fetch loop — before parsing. This prevents over-fetching from bleeding into the output.

### Type hints
Use `Optional[X]` from `typing`, not `X | None`. The system runs Python 3.9.

---

## Design decisions

- **Scrapers do not filter by content** — filtering for keywords, topics, or relevance is the workflow's job. Scrapers return raw data.
- **Date filtering is done client-side** — fetch from the actor, then filter in Python. Don't rely on actor-side date params.
- **Output order is always newest → oldest** — all `apimaestro` actors return in this order natively. Don't sort unless the actor guarantees nothing.
- **No business logic in scrapers** — no scoring, ranking, or summarisation. Raw fields only.

---

## Standard files every scraper must have

| File | Purpose |
|---|---|
| `scraper.py` | The scraper module + CLI entry point |
| `input_schema.json` | Defines inputs with types, defaults, descriptions |
| `output_schema.json` | Defines the output structure |
| `example_input.json` | A real, runnable input example |
| `example_output.json` | A realistic hand-crafted output example |
| `raw_sample.json` | Raw response from a live 1-item discovery call |
| `README.md` | Inputs table, outputs example, usage commands |

`raw_sample.json` must be a real API response, not a placeholder. If it's a placeholder, mark it clearly and treat the scraper as unverified.

---

## Writing results to Google Sheets

- **Only write URLs to the sheet** — never write full post content. Sheets are for references and links, not raw data dumps.
- **Store full content locally** — save the complete JSON output to a local file (e.g. `test_results.json`) alongside the scraper.
- This keeps sheets clean and readable while preserving all data for downstream processing.

---

## Exa (Web Search) — key patterns

- **No Apify** — Exa is a direct Python SDK (`exa_py`), not an Apify actor. No discovery call needed, no `raw_sample.json` process.
- **Use `search(query, contents={...})` with `highlights + summary`** — never fetch full `text`. Highlights + summary gives rich signal at a fraction of the cost. (The old top-level `search_and_contents()` kwargs are deprecated; contents now go under the `contents=` arg.)
- **No native batch API** — Exa has no batch endpoint. Use `ThreadPoolExecutor` with `max_workers=5` to parallelise multiple queries. Tested: 3 queries go from 26s sequential → 9s parallel (~3x speedup). This compounds significantly at 10+ queries.
- **Scraper already has `search_web_batch(queries=[...])`** — workflows should always use this instead of looping over `search_web`.
- **`use_autoprompt` is not a valid option** in the current exa_py SDK version — do not pass it.

---

## Credentials

- Live in the repo-root `.env` (copy `.env.example` to `.env` and fill in). `config.py` loads it via `load_dotenv(REPO_ROOT/.env, override=True)`; scrapers load it via `../../.env`.
- Keys: `APIFY_API_TOKEN`, `EXA_API_KEY` (plus `ANTHROPIC_API_KEY`, `APOLLO_API_KEY`, `FIRECRAWL_API_KEY` per workflow).

---

## Discovery call failures — read before retrying
If a discovery call fails with a validation error, stop and read the exact error message before trying again. The error usually tells you exactly what's wrong (e.g. `input.urls is required`). Then check LEARNINGS.md for the provider's known input format. Only then run again — never guess twice.

---

## Website Scraper — key patterns

- **No external APIs** — uses `requests` + `BeautifulSoup4` only. No Apify, no Exa.
- **Jina Reader fallback** — if direct fetch returns thin content (<200 chars), automatically retries via `https://r.jina.ai/{url}`. Free, no API key. Fixes JS-heavy SPAs (e.g. Framer sites). Never add Playwright — the dependency cost isn't worth it when Jina covers the same cases.
- **Direct path fallback** — if nav link discovery finds <2 pages, the scraper tries common paths directly (`/about`, `/pricing`, `/careers`, etc.) via HEAD requests. Fixes sites where key pages aren't in the nav.
- **Customer extraction from logo images** — company names are often only in `<img alt>` attributes or src filenames (e.g. `logo-confluent.svg`), never in visible text. Always run `_extract_customers_from_images()` across all scraped pages. This is how we got 44 Glean customers and 11 Airwallex customers that plain text extraction missed entirely.
- **`raw_sample.json` is the live scrape output** — no API discovery call needed. Just run `python3 scraper.py example_input.json > raw_sample.json`.
- **`full_text_by_page` is the reliable output** — structured fields (customers, icp_hints, etc.) are heuristic and noisy. Workflows should pass `full_text_by_page` to an LLM with specific questions rather than trusting the extracted fields directly.
- **JS-interactive pages still fail** — Airwallex product tour, region-selector-heavy homepages. Accept this limitation. Jina can't interact with JS.

---

## Twitter / X scrapers — key patterns (both profile + research)

**Actor: `kaitoeasyapi/twitter-x-data-tweet-scraper-pay-per-result-cheapest`** (swapped in 2026-06-26).
Replaced `altimis/scweet`, which started demanding *full X account access* — a non-starter for an OSS tool (every user would have to log their personal X account into a third-party actor → ban risk). The kaitoeasyapi actor uses **public guest tokens — no login, no cookies**, works on the **Apify free plan**, and is among the cheapest (~$0.18-0.25 / 1,000 tweets).

- **One actor, two modes** — keyword search and profile scraping both go through `searchTerms` (array):
  - Keyword search: `{"searchTerms": ["AI sales"], "sort": "Latest", "maxItems": N}`
  - Profile scrape: `{"searchTerms": ["from:paulg"], ...}` — use the `from:<handle>` operator. Extract the handle from the profile URL with a regex.
- **Date filtering:** append Twitter's native `since:YYYY-MM-DD until:YYYY-MM-DD` operators to the query string (NOT separate input fields). Reliable, server-side.
- **`maxItems` minimum is 20** — actor bills a 20-item minimum per run. Use `max(max_tweets, 20)`.
- **Flat output structure** (NOT scweet's nested `user`/`tweet`): top-level `id`, `text`, `createdAt`, `url`/`twitterUrl` (x.com — normalize to twitter.com), `likeCount`, `retweetCount`, `replyCount`, `quoteCount`, `bookmarkCount`, `viewCount`, `isReply`, `lang`, `entities.hashtags[].text`, `entities.user_mentions[].screen_name`. Author block under `author{}`: `userName`, `name`, `followers`, `following`, `statusesCount`, `isVerified`/`isBlueVerified`, `profilePicture`, `createdAt`.
- **Profile is embedded in tweets** — no separate profile endpoint. Extract `item["author"]` from the first item with a `userName`.
- **Quote/retweet detection:** `is_quote = bool(item["quoted_tweet"] or item["quoted_tweet_results"])`; `is_retweet = bool(item["retweeted_tweet"]) or text.startswith("RT @")`.
- **Date format unchanged:** `"Fri Jun 26 13:45:15 +0000 2026"` — same `"%a %b %d %H:%M:%S +0000 %Y"` parser as before.
- **Bio/website are usually present** but can be empty for some accounts. Verified: `from:sama` returns full `description`+`location`; `from:paulg` came back empty (cause unconfirmed — account-specific or intermittent, NOT a systematic limitation). `name`/`followers`/`verified`/tweets are always present. Consumers don't read bio anyway, so it's a non-issue either way.
- **Rate limit:** can return 0 items on *truly simultaneous* back-to-back runs. A small `time.sleep(5)` between batch calls (was 30s for scweet) is sufficient — verified 3 consecutive queries all returned full results. The actor rotates guest tokens internally.
- **Rejected actors** (each note marks whether it was *run* or judged from *docs*):
  - `apidojo/tweet-scraper` — RAN 2026-06-26: blocks free-plan API (returns `{noResults: true}` + "subscribe to a paid plan"). Confirmed.
  - `altimis/scweet` — RAN in prior QA: now demands full X account access (security risk). The actor we replaced.
  - `parseforge/x-com-scraper` — DOCS only: *works on free plan*, but keyword search requires a username scope (no standalone search) and no date range; $8/1k. Rejected on fit, not on free-plan failure.
  - `gentle_cloud/x-twitter-public-data-scraper` — DOCS only: no keyword search at all (lookup/profile modes only). Rejected on fit.
  - `xtdata/twitter-x-scraper` — earlier-session note: ignores maxItems (cost $2.93 for 718 tweets). Not re-run this session.
  - `apidojo/twitter-profile-scraper`, `quacker/twitter-scraper` — earlier-session notes: blocks free plan / 4GB browser crawler $0.35/run, inferior data. Not re-run this session.

---

## YC Startup Directory Scraper — key patterns

- **No Apify** — uses YC's free public API (`api.ycombinator.com/v0.1/companies`). No API key needed.
- **API is capped** — returns ~44 companies per batch max, not the full directory (website shows 187 for W23). The API intentionally limits results. Accept this limitation.
- **Pagination:** `page` param, 20 companies per page regardless of `count` value. Always paginate via `totalPages`.
- **Filters:** `batch` works server-side. `industry`, `location`, `status` must be filtered client-side after fetching all batch companies.
- **Founder details** — not in the list API. Fetch from individual company pages via Jina Reader (`https://r.jina.ai/https://www.ycombinator.com/companies/{slug}`). Parse with regex — each founder appears twice in the Jina output, deduplicate by name.
- **Founder fetch is optional** — default `include_founders=False` for speed. Use `ThreadPoolExecutor(max_workers=5)` when fetching founders in parallel.

---

## Product Hunt Scraper — key patterns

- **No Apify, no proxy** — uses Product Hunt's public RSS feed (`producthunt.com/feed`). Free, no API key.
- **Cloudflare blocks everything else** — individual product pages, topic-specific feeds (`/topics/{topic}/feed` returns 403), all Apify actors, and Jina Reader all return 403. RSS is the only working endpoint.
- **RSS hard cap: 50 entries** — no pagination. Always the 50 most recent launches across all topics.
- **No upvotes, topics, or maker details** — these are only available on individual pages which are Cloudflare-blocked. Not buildable on free plan.
- **Fields available:** name, tagline (from HTML content), author (submitter), published date, PH URL, post ID.

---

## Review Scraper — key patterns

- **G2:** `zen-studio/g2-reviews-scraper` — input field is `url` (not `productUrl`, not `startUrls`). Returns rich fields: reviewer name, title, star rating, text, date, verified status, incentivized flag. Pay per event — check pricing before running.
- **Trustpilot:** Jina Reader on `trustpilot.com/review/{domain}` — works, free. Returns ~30 reviews per page plus overall TrustScore, total review count, and AI-generated summary. Parse with regex on Jina markdown output.
- **Capterra:** Blocked everywhere — Cloudflare blocks datacenter IPs, Jina, and all Apify actors. Not buildable on free plan.
- **Rejected actors:**
  - `focused_vanguard/multi-platform-reviews-scraper` — **$7.99/1,000 results**. Ran without checking price. Burned credits.
  - `zen-studio/software-review-scraper` — **$4.99/1,000 reviews**. Same mistake. Always check pricing first.
  - `imadjourney/capterra-reviews-scraper` — requires rental, not on free plan.
  - `omkar-cloud/g2-product-scraper` — requires rental, free trial expired.
- **Folder naming** — name scrapers for their full scope, not just one platform. "Review Scraper" not "G2 Scraper".

---

## LinkedIn Post Comment Scraper — key patterns

- **Actor:** `apimaestro/linkedin-post-comments-replies-engagements-scraper-no-cookies`
- **Pricing:** $1.2/1,000 comments ($0.0012/comment). No minimum charge per run.
- **Input field:** `postIds` (array) — accepts full LinkedIn post URLs or bare numeric activity IDs. Extract ID from URL via regex: `activity-(\d+)`.
- **Pagination:** `page` param, 100 comments per page. Auto-paginate until `max_comments` is hit.
- **`comment_type`** — `"comment"` for top-level, `"reply"` for nested. Filter replies client-side with `include_replies` param.
- **Key output fields:** `comment_id`, `text`, `posted_at.date`, `comment_url`, `comment_type`, `author.name`, `author.headline`, `author.profile_url`, `stats.total_reactions`, `stats.reactions{}`, `stats.comments` (reply count).

---

## LinkedIn Post Research Scraper — key patterns

- **Actor:** `harvestapi/linkedin-post-search` (swapped 2026-07-09; `apimaestro/linkedin-posts-search-scraper-no-cookies` is the fallback)
- **Pricing:** $0.005/post on free tier (PAY_PER_EVENT). For 20 posts = ~$0.10/search.
- **Input fields:**
  - `keyword` (string) — the search term. NOT `keywords` (array) or `query`.
  - `sort_type` — `"relevance"` or `"date_posted"`. NOT `"date"` (validation error).
  - `page_number` — 1-indexed. Actor always returns 50 per page; cap client-side with `maxPosts`.
  - `maxPosts` — caps total items returned.
  - `date_filter` — undocumented by actor; leave empty string or omit.
- **Output structure:** each item includes `activity_id`, `post_url`, `text`, `author{}`, `stats{}`, `posted_at{}`, `hashtags[]`, `content{}`, `is_reshare`, `metadata{}`, `search_input`.
- **`metadata` on first item** — contains `total_count` (total LinkedIn results for query), `page`, `has_next_page`.
- **`posted_at.date`** — `"YYYY-MM-DD HH:MM:SS"` string. `posted_at.timestamp` is Unix ms.
- **`stats`** — `total_reactions`, `comments`, `shares`. Reactions breakdown in `reactions[]`.
- **Dataset item count misleading** — run `stats.itemCount` shows 0 even when data is present. Always fetch from dataset directly.
- ~~Discovery trap: harvestapi/linkedin-post-search is profile-filtering only~~ — **outdated (fixed upstream)**: since mid-2026 it accepts `searchQueries` for keyword search and is now our primary post-search actor ($2/1k vs apimaestro's $5/1k).
- **`benjarapi/linkedin-post-search`** and **`powerai/linkedin-posts-search-scraper`** exist as alternatives but are less proven.

---

## Reddit Research Scraper — key patterns

- **No Apify** — uses Reddit's free public JSON API. No API key needed.
- **Endpoints:** `reddit.com/search.json` (all Reddit) or `reddit.com/r/{subreddit}/search.json` (subreddit-scoped). Pass `restrict_sr=true` for subreddit-scoped search.
- **Key params:** `q` (query), `sort` (new/top/relevance/hot/comments), `t` (hour/day/week/month/year/all), `limit` (max 100), `after` (pagination cursor).
- **Pagination:** cursor-based via `after` field in response. Keep fetching until `after` is null or desired count reached.
- **Best combos:** `sort=top&t=month` for high-signal posts; `sort=new&t=week` for real-time monitoring.
- **Exact phrase search** — wrap query in quotes: `"context graph"` for precision. Broad queries return noisy results.
- **Rate limit:** ~60 req/min unauthenticated. Add `time.sleep(0.5)` between paginated calls to be polite.
- **User-Agent required** — always pass `User-Agent: GTMEngine/1.0` header or requests get blocked.

---

## Token efficiency (Claude usage)

Each scraper should cost well under 10% of monthly Claude usage. Avoid:

- **Spawning subagents** for actor research — use an inline `WebSearch` call instead.
- **WebFetch on Apify store pages** — they're JS-rendered and return CSS garbage. Always use the REST API.
- **Hunting for the API token** — it's in the location above, always.
- **Reading files you don't need** — only read the existing scraper you're modelling from, not all of them.
