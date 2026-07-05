import os
import sys
import json
import time
import threading
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, List
from exa_py import Exa

DEFAULT_NUM_RESULTS = 5
DEFAULT_SUMMARY_QUESTION = "What is most important or notable about this?"

# Exa's plan caps requests at ~10/second. Workflows fan out searches across
# threads (and small-talk fans out again internally), so concurrent callers can
# easily burst past the cap and eat 429s — a 429 is a wasted paid call. A
# process-wide limiter spaces call *starts* so the burst rate stays under 10/s
# no matter how many threads call search_web at once.
_EXA_MIN_INTERVAL = 0.12  # ~8.3 req/s, safely under the 10/s cap
_exa_lock = threading.Lock()
_exa_next = 0.0


def _exa_throttle() -> None:
    global _exa_next
    with _exa_lock:
        now = time.monotonic()
        wait = _exa_next - now
        if wait > 0:
            time.sleep(wait)
            now += wait
        _exa_next = now + _EXA_MIN_INTERVAL


def search_web(
    query: str,
    num_results: int = DEFAULT_NUM_RESULTS,
    days_back: Optional[int] = None,
    include_domains: Optional[list] = None,
    exclude_domains: Optional[list] = None,
    summary_question: Optional[str] = None,
) -> dict:
    """
    Fetches web results and structured insights for a query using Exa.

    Uses highlights + summary mode — no full page text is fetched, keeping
    costs and token usage minimal.

    Args:
        query:            Search query (company name, person, topic)
        num_results:      Number of results (default 5, max 20)
        days_back:        Only return results published in last N days
        include_domains:  Restrict to these domains
        exclude_domains:  Exclude these domains
        summary_question: Question Exa answers per result from its content

    Returns:
        dict with keys: query, total, results, errors
    """
    api_key = os.environ.get("EXA_API_KEY")
    if not api_key:
        raise EnvironmentError("EXA_API_KEY environment variable is not set.")

    exa = Exa(api_key)

    question = summary_question or DEFAULT_SUMMARY_QUESTION

    kwargs = {
        "num_results": num_results,
        # Exa "fast" search type — same content index, p50 latency <425ms.
        # Available on the free plan; full param compatibility with contents.
        "type": "fast",
        "highlights": {"num_sentences": 3, "highlights_per_url": 2},
        "summary": {"query": question},
    }

    if days_back is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%dT%H:%M:%SZ")
        kwargs["start_published_date"] = cutoff

    if include_domains:
        kwargs["include_domains"] = include_domains

    if exclude_domains:
        kwargs["exclude_domains"] = exclude_domains

    print(f"Searching: {query!r} ({num_results} results)")

    _exa_throttle()
    response = exa.search_and_contents(query, **kwargs)

    # Deduplicate by URL
    seen_urls = set()
    results = []
    for r in response.results:
        if r.url in seen_urls:
            continue
        seen_urls.add(r.url)
        results.append({
            "title": r.title,
            "url": r.url,
            "published_date": r.published_date,
            "author": r.author,
            "summary": r.summary,
            "highlights": r.highlights or [],
        })

    return {
        "query": query,
        "total": len(results),
        "results": results,
        "errors": [],
    }


def search_web_batch(
    queries: List[str],
    num_results: int = DEFAULT_NUM_RESULTS,
    days_back: Optional[int] = None,
    include_domains: Optional[list] = None,
    exclude_domains: Optional[list] = None,
    summary_question: Optional[str] = None,
    max_workers: int = 5,
) -> List[dict]:
    """
    Runs multiple search_web calls in parallel using threads.
    ~3x faster than sequential for 3+ queries.

    Returns a list of results in the same order as the input queries.
    """
    def _fetch(query):
        return search_web(
            query=query,
            num_results=num_results,
            days_back=days_back,
            include_domains=include_domains,
            exclude_domains=exclude_domains,
            summary_question=summary_question,
        )

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        return list(pool.map(_fetch, queries))


if __name__ == "__main__":
    # Load .env from GTM Engine root
    env_path = os.path.join(os.path.dirname(__file__), "../../.env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())

    input_file = sys.argv[1] if len(sys.argv) > 1 else "example_input.json"
    with open(input_file) as f:
        inp = json.load(f)

    result = search_web(
        query=inp["query"],
        num_results=inp.get("num_results", DEFAULT_NUM_RESULTS),
        days_back=inp.get("days_back"),
        include_domains=inp.get("include_domains"),
        exclude_domains=inp.get("exclude_domains"),
        summary_question=inp.get("summary_question"),
    )

    print(json.dumps(result, indent=2, ensure_ascii=False))
