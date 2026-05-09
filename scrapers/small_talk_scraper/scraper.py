"""
Small Talk Intelligence Scraper

Finds humanizing, conversational details about a prospect that can be used
naturally as small talk in cold outbound. Optimizes for HUMANITY, not enrichment.

Pipeline:
  1. Identity resolution  — find Twitter handle if not given, verify it.
  2. Signal harvest       — fetch recent tweets + run targeted web searches
                            across human-signal categories.
  3. Extraction           — Claude pulls humanizing signals (hobbies, fandoms,
                            quirks, humor, routines), excluding business stuff.
  4. Scoring & selection  — Claude scores small-talk-worthiness; top 2-3 win.
  5. Output               — 2-3 line string + structured signals + identity.

Workflow-facing entrypoint: scrape_small_talk(profile_url, name, company, ...)
returns {"small_talk": str, "signals": [...], "identity": {...}}.
"""

import os
import sys
import json
import re
from typing import Optional, List, Dict, Any

import anthropic

# Make sibling scrapers importable when run as a module from repo root
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from config import CLAUDE_MODEL  # noqa: E402
from scrapers.web_search.scraper import search_web, search_web_batch  # noqa: E402
from scrapers.twitter_profile_scraper.scraper import scrape_twitter_profile  # noqa: E402


DEFAULT_NUM_SEARCH_QUERIES = 5
DEFAULT_TWEETS_TO_PULL = 60
DEFAULT_TWITTER_DAYS_BACK = 180
DEFAULT_MAX_SIGNALS = 3


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _strip_json_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _claude_json(client: anthropic.Anthropic, prompt: str, max_tokens: int = 1500) -> Any:
    resp = client.messages.create(
        model=CLAUDE_MODEL, max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return json.loads(_strip_json_fence(resp.content[0].text))


def _twitter_handle_from_url(url: str) -> Optional[str]:
    if not url:
        return None
    m = re.search(r"(?:twitter\.com|x\.com)/([A-Za-z0-9_]{1,15})", url)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Stage 1 — Identity resolution
# ---------------------------------------------------------------------------

def resolve_identity(
    name: str,
    company: str,
    linkedin_url: str = "",
    twitter_url: str = "",
    website: str = "",
    client: Optional[anthropic.Anthropic] = None,
) -> Dict[str, Any]:
    """Build an identity graph. Most important: lock in the right Twitter handle."""
    identity: Dict[str, Any] = {
        "name": name,
        "company": company,
        "linkedin_url": linkedin_url,
        "twitter_url": twitter_url,
        "website": website,
        "twitter_confidence": 1.0 if twitter_url else 0.0,
    }

    if twitter_url or not client:
        return identity

    # Look for the handle on twitter/x via Exa
    try:
        result = search_web(
            query=f'"{name}" {company} twitter',
            num_results=5,
            include_domains=["twitter.com", "x.com"],
            summary_question=f"Is this the Twitter/X profile of {name} who works at {company}? What is their bio?",
        )
    except Exception as e:
        print(f"  identity search failed: {e}", file=sys.stderr)
        return identity

    candidates = []
    for r in result.get("results", []):
        handle = _twitter_handle_from_url(r.get("url", ""))
        if not handle or handle.lower() in {"home", "search", "explore", "i"}:
            continue
        candidates.append({
            "handle": handle,
            "url": f"https://twitter.com/{handle}",
            "title": r.get("title", ""),
            "summary": r.get("summary", ""),
        })

    if not candidates:
        return identity

    prompt = f"""You are matching a person to a Twitter/X profile.

Person:
- Name: {name}
- Company: {company}

Candidate profiles:
{json.dumps(candidates, indent=2, ensure_ascii=False)}

Pick the candidate most likely to be this person — cross-reference name AND company in the bio/title.
If none clearly match, return null.

Return JSON: {{"handle": "<handle or null>", "confidence": <0.0-1.0>, "reason": "<one line>"}}
Only valid JSON."""

    try:
        pick = _claude_json(client, prompt, max_tokens=300)
        if pick.get("handle"):
            identity["twitter_url"] = f"https://twitter.com/{pick['handle']}"
            identity["twitter_confidence"] = float(pick.get("confidence") or 0.0)
            identity["twitter_match_reason"] = pick.get("reason", "")
    except Exception as e:
        print(f"  identity resolution LLM failed: {e}", file=sys.stderr)

    return identity


# ---------------------------------------------------------------------------
# Stage 2 — Signal harvest
# ---------------------------------------------------------------------------

SIGNAL_CATEGORIES = [
    "hobbies, sports, fandoms (e.g. F1, Arsenal, anime, climbing)",
    "gaming, music, books, movies, podcasts they like",
    "internet behavior — meme posting, humor style, recurring jokes",
    "lifestyle — coffee, fitness, travel, routines, weekend activities",
    "side projects, weird quirks, unusual opinions, identity signals",
]


def generate_search_queries(
    name: str,
    company: str,
    twitter_handle: Optional[str],
    client: anthropic.Anthropic,
    n: int = DEFAULT_NUM_SEARCH_QUERIES,
) -> List[str]:
    """Have Claude generate targeted queries aimed at humanizing signals."""
    cats = "\n".join(f"- {c}" for c in SIGNAL_CATEGORIES)
    prompt = f"""Generate {n} web search queries to find HUMANIZING details about a person —
hobbies, fandoms, quirks, humor, casual interests. NOT business/professional info.

Person: {name}
Company: {company}
{f"Twitter handle: @{twitter_handle}" if twitter_handle else ""}

Aim queries at categories like:
{cats}

Each query should be specific and target casual/personal content (interviews,
podcasts, blog posts, Reddit, YouTube — anything where they speak conversationally).
AVOID generic queries like "{name} CEO" or "{name} {company} interview about funding".

Return JSON array of {n} strings. Only valid JSON."""

    try:
        queries = _claude_json(client, prompt, max_tokens=600)
        if isinstance(queries, list):
            return [str(q) for q in queries][:n]
    except Exception as e:
        print(f"  query generation failed: {e}", file=sys.stderr)

    # Fallback queries
    base = f'"{name}" {company}'
    return [
        f"{base} podcast interview",
        f"{base} hobbies",
        f"{base} personal blog",
        f'"{name}" reddit OR twitter casual',
        f"{base} weekend OR side project",
    ][:n]


def harvest_signals(
    identity: Dict[str, Any],
    client: anthropic.Anthropic,
    max_tweets: int = DEFAULT_TWEETS_TO_PULL,
    days_back: int = DEFAULT_TWITTER_DAYS_BACK,
    num_queries: int = DEFAULT_NUM_SEARCH_QUERIES,
    skip_twitter: bool = False,
) -> Dict[str, Any]:
    """Pull tweets + run targeted web searches. Returns raw corpus + metadata."""
    name = identity["name"]
    company = identity["company"]
    twitter_url = identity.get("twitter_url") or ""
    handle = _twitter_handle_from_url(twitter_url)

    tweets: List[Dict] = []
    twitter_profile = None
    if handle and not skip_twitter:
        try:
            tw = scrape_twitter_profile(
                profile_url=twitter_url,
                max_tweets=max_tweets,
                days_back=days_back,
                include_retweets=False,
            )
            tweets = tw.get("tweets", []) or []
            twitter_profile = tw.get("profile")
        except Exception as e:
            print(f"  twitter scrape failed: {e}", file=sys.stderr)

    queries = generate_search_queries(name, company, handle, client, n=num_queries)
    try:
        search_results = search_web_batch(queries, num_results=4)
    except Exception as e:
        print(f"  web search batch failed: {e}", file=sys.stderr)
        search_results = []

    return {
        "twitter_profile": twitter_profile,
        "tweets": tweets,
        "search_queries": queries,
        "search_results": search_results,
    }


# ---------------------------------------------------------------------------
# Stage 3 + 4 — Extract & score
# ---------------------------------------------------------------------------

EXTRACTION_SYSTEM = """You extract ONLY humanizing, conversational details about a person —
the kind of thing a friend would mention casually at dinner, NOT a CRM field.

GOOD signals:
- "Arsenal fan", "Big into F1", "Marathon runner", "Coffee nerd"
- "DJed in high school", "Plays Valorant", "Reads sci-fi"
- "Posts cat pictures", "Late-night philosophical tweets"
- "Loves Interstellar", "Tweets through cricket matches"

BAD signals (REJECT these):
- "Raised funding", "CEO at company", "Hiring engineers"
- "Passionate about AI", "Building the future", "Startup operator"
- Any company achievements, professional accolades, or generic motivational fluff.

CRITICAL — IDENTITY VERIFICATION:
The internet is full of people with the same name. Before accepting ANY evidence,
you MUST verify the source is about the SAME person we're researching.
A source is acceptable ONLY if it does ONE of these:
  (a) Explicitly mentions the target company by name, OR
  (b) Explicitly references their LinkedIn URL / handle, OR
  (c) Contains a biographical detail (role title, employer history, location)
      that is uniquely consistent with the person's LinkedIn profile.
If a source is just "<same first/last name> personal website" with NO mention
of the target company and NO biographical anchor matching the LinkedIn — REJECT IT.
A wrong-person signal is worse than no signal. When in doubt, reject.

DEDUP & CONSOLIDATION:
- Each signal must come from a UNIQUE source URL. Never cite the same URL twice.
- Each signal must be a DIFFERENT TOPIC. "Hard sciences nerd" and "physics
  background" are the same topic — pick the single strongest version.
- Tweets/posts are valuable; same casual tweets > polished posts."""


def extract_and_score_signals(
    identity: Dict[str, Any],
    harvest: Dict[str, Any],
    client: anthropic.Anthropic,
    max_signals: int = DEFAULT_MAX_SIGNALS,
) -> List[Dict[str, Any]]:
    name = identity["name"]
    company = identity["company"]

    # Build a compact corpus for Claude
    tweet_lines = []
    for t in harvest.get("tweets", [])[:80]:
        text = (t.get("text") or "").replace("\n", " ").strip()
        if not text:
            continue
        tag = "REPLY" if t.get("is_reply") else ("QUOTE" if t.get("is_quote") else "TWEET")
        tweet_lines.append(f"[{tag} {t.get('created_at','')[:10]}] {text}  <{t.get('url','')}>")

    search_blocks = []
    for sr in harvest.get("search_results", []):
        for r in sr.get("results", [])[:4]:
            search_blocks.append({
                "query": sr.get("query", ""),
                "url": r.get("url"),
                "title": r.get("title"),
                "summary": r.get("summary"),
                "highlights": r.get("highlights", []),
                "published": r.get("published_date"),
            })

    corpus = {
        "tweets": tweet_lines[:80],
        "web": search_blocks[:30],
    }

    linkedin_url = identity.get("linkedin_url", "")
    twitter_url = identity.get("twitter_url", "")

    prompt = f"""{EXTRACTION_SYSTEM}

TARGET PERSON:
- Name:     {name}
- Company:  {company}
- LinkedIn: {linkedin_url or "(unknown)"}
- Twitter:  {twitter_url or "(unknown)"}

Below is raw material gathered by web search and tweet scraping. The internet
is messy — many results will be about other people who share this name.
Apply the IDENTITY VERIFICATION rules from the system above to every piece
of evidence before keeping it. Reject anything you can't anchor to {company}
or to a biographical detail consistent with the LinkedIn profile.

Raw material (JSON):
{json.dumps(corpus, indent=2, ensure_ascii=False)[:18000]}

For each signal return:
- topic              (short label, max 8 words — e.g. "F1 fan", "Coffee nerd", "Plays Valorant")
- evidence_quote     (short verbatim quote — under 120 chars, trimmed)
- source_url         (the EXACT URL — must be unique across all signals)
- source_type        ("twitter" | "podcast" | "interview" | "blog" | "reddit" | "youtube" | "other")
- identity_anchor    (one short phrase explaining how you verified this source is the SAME person —
                      e.g. "page mentions {company}", "bio matches LinkedIn role", "tweet from verified handle")
- confidence         (0.0-1.0)
- small_talk_score   (0-10)

Hard rules for the returned list:
- At most {max_signals} signals.
- Every signal has a UNIQUE source_url. NEVER cite the same URL twice.
- Every signal covers a DIFFERENT FACT. Not just different topic labels —
  different facts. If signal A's evidence quote already contains signal B's
  fact, they are duplicates: collapse them into ONE signal with a richer label.
  Example: a LinkedIn post that says "I wrestled for Penn State and did cage
  fighting" is ONE signal ("Combat sports — Penn State wrestler + cage fighter"),
  not two.
- Each evidence_quote must contain ONLY the fact for ITS signal. Trim it.
  If the natural quote bundles multiple facts, either collapse the signals
  or cut the quote to the relevant clause.
- Reject any signal whose identity_anchor is weak or absent.
- If genuinely humanizing AND identity-verified signals are fewer than {max_signals},
  return fewer. NEVER pad. Empty array is a valid answer.

Return JSON: {{"signals": [{{...}}, ...]}}
Only valid JSON, no commentary."""

    try:
        out = _claude_json(client, prompt, max_tokens=2000)
        signals = out.get("signals", []) if isinstance(out, dict) else []
    except Exception as e:
        print(f"  signal extraction failed: {e}", file=sys.stderr)
        return []

    # Final sort + truncate defensively
    signals.sort(key=lambda s: s.get("small_talk_score", 0), reverse=True)
    return signals[:max_signals]


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def format_small_talk_string(signals: List[Dict[str, Any]]) -> str:
    """Render top signals as compact 1-line bullets with source. Dedupes URLs."""
    lines = []
    seen_urls = set()
    seen_topics = set()
    for s in signals:
        topic = (s.get("topic") or "").strip()
        url = (s.get("source_url") or "").strip()
        if not topic:
            continue
        topic_key = topic.lower()
        if url and url in seen_urls:
            continue
        if topic_key in seen_topics:
            continue
        seen_urls.add(url)
        seen_topics.add(topic_key)

        src = (s.get("source_type") or "").strip()
        evidence = (s.get("evidence_quote") or "").strip().replace("\n", " ")
        if len(evidence) > 100:
            evidence = evidence[:100].rstrip() + "…"
        suffix_bits = []
        if evidence:
            suffix_bits.append(f'"{evidence}"')
        if url:
            suffix_bits.append(f"[{src}: {url}]" if src else f"[{url}]")
        suffix = " — " + " ".join(suffix_bits) if suffix_bits else ""
        lines.append(f"- {topic}{suffix}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Workflow-facing entrypoint
# ---------------------------------------------------------------------------

def scrape_small_talk(
    profile_url: str = "",
    name: str = "",
    company: str = "",
    twitter_url: str = "",
    website: str = "",
    max_signals: int = DEFAULT_MAX_SIGNALS,
    max_tweets: int = DEFAULT_TWEETS_TO_PULL,
    days_back: int = DEFAULT_TWITTER_DAYS_BACK,
    num_queries: int = DEFAULT_NUM_SEARCH_QUERIES,
    skip_twitter: bool = False,
) -> Dict[str, Any]:
    """
    Find humanizing small-talk signals for a person.

    Args:
        profile_url:   LinkedIn URL (workflow's primary handle).
        name:          Full name.
        company:       Company.
        twitter_url:   Twitter/X URL if known. Otherwise resolved from search.
        website:       Personal website if known.
        max_signals:   Max signals to return (default 3).
        max_tweets:    Tweets to pull (default 60).
        days_back:     Twitter window in days (default 180).
        num_queries:   Targeted web queries to generate (default 5).
        skip_twitter:  Skip Twitter scraping (saves Apify cost).

    Returns:
        {
          "small_talk": "<2-3 line string with source>",
          "signals":    [{topic, evidence_quote, source_url, ...}, ...],
          "identity":   {name, company, linkedin_url, twitter_url, ...},
        }
    """
    if not name:
        return {"small_talk": "", "signals": [], "identity": {}}

    client = anthropic.Anthropic()

    identity = resolve_identity(
        name=name, company=company,
        linkedin_url=profile_url, twitter_url=twitter_url, website=website,
        client=client,
    )

    harvest = harvest_signals(
        identity, client,
        max_tweets=max_tweets, days_back=days_back, num_queries=num_queries,
        skip_twitter=skip_twitter,
    )

    signals = extract_and_score_signals(identity, harvest, client, max_signals=max_signals)

    return {
        "small_talk": format_small_talk_string(signals),
        "signals": signals,
        "identity": identity,
    }


if __name__ == "__main__":
    input_file = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(__file__), "example_input.json",
    )
    with open(input_file) as f:
        inp = json.load(f)

    result = scrape_small_talk(
        profile_url=inp.get("profile_url", ""),
        name=inp.get("name", ""),
        company=inp.get("company", ""),
        twitter_url=inp.get("twitter_url", ""),
        website=inp.get("website", ""),
        max_signals=inp.get("max_signals", DEFAULT_MAX_SIGNALS),
        max_tweets=inp.get("max_tweets", DEFAULT_TWEETS_TO_PULL),
        days_back=inp.get("days_back", DEFAULT_TWITTER_DAYS_BACK),
        num_queries=inp.get("num_queries", DEFAULT_NUM_SEARCH_QUERIES),
        skip_twitter=inp.get("skip_twitter", False),
    )

    print(json.dumps(result, indent=2, ensure_ascii=False))
