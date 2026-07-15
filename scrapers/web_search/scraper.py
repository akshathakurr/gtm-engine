import os
import sys
import json
import time
import socket
import threading
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, List
from urllib.parse import urlparse

DEFAULT_NUM_RESULTS = 5
DEFAULT_SUMMARY_QUESTION = "What is most important or notable about this?"

# Hard cap on any single Exa socket op (connect/read). The SDK offers no timeout
# knob, so without this a hung connection wedges the calling thread indefinitely.
EXA_SOCKET_TIMEOUT = float(os.environ.get("EXA_SOCKET_TIMEOUT") or 45)

# ---------------------------------------------------------------------------
# Provider selection
# ---------------------------------------------------------------------------
# Two interchangeable search backends sit behind search_web():
#   - Exa      (EXA_API_KEY)      — semantic search, native date/domain filters
#   - Parallel (PARALLEL_API_KEY) — LLM-optimized excerpts, generous free tier
#
# SEARCH_PROVIDER controls which is used:
#   auto     (default) — use whichever key is set. If BOTH are set, use the
#                        primary and fall back to the other on any failure
#                        (rate limit, timeout, exhausted credits, etc.).
#   exa                — force Exa only.
#   parallel           — force Parallel only.
#   both               — query both and merge+dedup results (broader coverage).
#
# When both keys are present, SEARCH_PRIMARY picks the primary (default "exa" —
# Exa first, Parallel as the fallback; set to "parallel" to prefer Parallel). If
# a user only sets one key, that provider is used and the other is never touched.
#
# A provider that hits a HARD failure mid-run (402/credit exhausted, 401/403
# auth) is disabled for the rest of the process (see _disable_provider) so we
# fail straight over to the fallback instead of re-hitting a dead account on
# every subsequent search and burning time / the fallback's credits.

_VALID_PROVIDERS = ("exa", "parallel")

# Providers that hard-failed this run — skipped by every later search_web call.
_disabled_providers = set()
_disabled_lock = threading.Lock()

# Substrings that mark an UNRECOVERABLE provider failure (vs. a transient blip).
# On these we stop using the provider for the rest of the run rather than retry.
_HARD_FAIL_MARKERS = (
    "402", "401", "403", "payment required", "insufficient", "quota",
    "credit", "exhaust", "unauthorized", "forbidden", "out of credits",
)


def _is_hard_fail(err) -> bool:
    s = str(err).lower()
    return any(m in s for m in _HARD_FAIL_MARKERS)


def _disable_provider(prov: str, err) -> None:
    """Latch a provider off for the rest of the run after a hard failure."""
    with _disabled_lock:
        if prov not in _disabled_providers:
            _disabled_providers.add(prov)
            print(f"  ⚠ disabling '{prov}' for the rest of this run after a hard "
                  f"failure ({err}); later searches skip it and use the fallback.")


def _resolve_providers():
    """Decide the provider(s) to use from config + available keys.

    Returns (mode, providers) where mode is one of 'single', 'fallback',
    'both' and providers is an ordered list drawn from _VALID_PROVIDERS.
    Raises EnvironmentError when the requested provider has no key.
    """
    exa_key = os.environ.get("EXA_API_KEY")
    par_key = os.environ.get("PARALLEL_API_KEY")
    setting = (os.environ.get("SEARCH_PROVIDER") or "auto").strip().lower()

    if setting == "exa":
        if not exa_key:
            raise EnvironmentError("SEARCH_PROVIDER=exa but EXA_API_KEY is not set.")
        mode, providers = "single", ["exa"]
    elif setting == "parallel":
        if not par_key:
            raise EnvironmentError("SEARCH_PROVIDER=parallel but PARALLEL_API_KEY is not set.")
        mode, providers = "single", ["parallel"]
    else:
        available = [p for p, k in (("exa", exa_key), ("parallel", par_key)) if k]
        if setting == "both":
            if not available:
                raise EnvironmentError(
                    "SEARCH_PROVIDER=both but neither EXA_API_KEY nor PARALLEL_API_KEY is set."
                )
            mode, providers = "both", available
        elif not available:  # auto, no keys
            raise EnvironmentError(
                "No search key set. Add EXA_API_KEY and/or PARALLEL_API_KEY to .env."
            )
        elif len(available) == 1:
            mode, providers = "single", available
        else:
            # Both keys present: Exa primary by default, Parallel as fallback.
            primary = (os.environ.get("SEARCH_PRIMARY") or "exa").strip().lower()
            if primary not in _VALID_PROVIDERS:
                primary = "exa"
            providers = [primary] + [p for p in ("exa", "parallel") if p != primary]
            mode = "fallback"

    # Drop any provider that hard-failed earlier this run so we don't re-hit a
    # dead account. If that leaves nothing, everything usable is exhausted.
    with _disabled_lock:
        active = [p for p in providers if p not in _disabled_providers]
    if not active:
        raise EnvironmentError(
            "All configured search providers are disabled this run (exhausted "
            "credits or auth failure). Top up or fix EXA_API_KEY / PARALLEL_API_KEY."
        )
    if mode == "fallback" and len(active) == 1:
        mode = "single"
    return mode, active


def _dedup(results: List[dict]) -> List[dict]:
    seen = set()
    out = []
    for r in results:
        url = r.get("url")
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(r)
    return out


# ---------------------------------------------------------------------------
# Exa backend
# ---------------------------------------------------------------------------
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


def _search_exa(
    query: str,
    num_results: int,
    days_back: Optional[int],
    include_domains: Optional[list],
    exclude_domains: Optional[list],
    question: str,
) -> List[dict]:
    """Run one Exa search and return a list of normalized result dicts."""
    api_key = os.environ.get("EXA_API_KEY")
    if not api_key:
        raise EnvironmentError("EXA_API_KEY environment variable is not set.")

    from exa_py import Exa  # lazy: Parallel-only users needn't have it imported

    exa = Exa(api_key)

    kwargs = {
        "num_results": num_results,
        # Exa "fast" search type — same content index, p50 latency <425ms.
        # Available on the free plan; full param compatibility with contents.
        "type": "fast",
        # search() takes content options under `contents` (highlights/summary/text);
        # the old top-level kwargs were the deprecated search_and_contents() style.
        "contents": {
            "highlights": {"num_sentences": 3, "highlights_per_url": 2},
            "summary": {"query": question},
        },
    }

    if days_back is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%dT%H:%M:%SZ")
        kwargs["start_published_date"] = cutoff

    if include_domains:
        kwargs["include_domains"] = include_domains

    if exclude_domains:
        kwargs["exclude_domains"] = exclude_domains

    _exa_throttle()
    # The exa_py SDK exposes no request timeout, so a stalled socket would hang
    # the worker thread (and, via the on_result callback, the whole run) forever.
    # Bound it with a socket-level read timeout for the duration of this call
    # only — Parallel sets its own explicit timeout, so leave the global default
    # untouched everywhere else.
    _prev_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(EXA_SOCKET_TIMEOUT)
    try:
        response = exa.search(query, **kwargs)
    finally:
        socket.setdefaulttimeout(_prev_timeout)

    return [
        {
            "title": r.title,
            "url": r.url,
            "published_date": r.published_date,
            "author": r.author,
            "summary": r.summary,
            "highlights": r.highlights or [],
        }
        for r in response.results
    ]


# ---------------------------------------------------------------------------
# Parallel backend
# ---------------------------------------------------------------------------
# https://docs.parallel.ai/search/search-quickstart
# POST /v1/search with x-api-key; returns results[] of {url,title,publish_date,
# excerpts[]}. Rate limit is 600 req/min (~10/s), so throttle like Exa. Parallel
# has no native date/domain filter, so those are applied client-side below.
_PARALLEL_ENDPOINT = "https://api.parallel.ai/v1/search"
_PARALLEL_MAX_CHARS = 1500  # per-excerpt cap — keeps tokens/cost minimal
_PAR_MIN_INTERVAL = 0.11  # ~9 req/s, under the 600/min cap
_par_lock = threading.Lock()
_par_next = 0.0


def _parallel_throttle() -> None:
    global _par_next
    with _par_lock:
        now = time.monotonic()
        wait = _par_next - now
        if wait > 0:
            time.sleep(wait)
            now += wait
        _par_next = now + _PAR_MIN_INTERVAL


def _host_matches(host: str, domain: str) -> bool:
    domain = domain.lower().lstrip(".")
    return host == domain or host.endswith("." + domain)


def _search_parallel(
    query: str,
    num_results: int,
    days_back: Optional[int],
    include_domains: Optional[list],
    exclude_domains: Optional[list],
    question: str,
) -> List[dict]:
    """Run one Parallel search and return a list of normalized result dicts.

    Parallel returns content excerpts rather than a per-result summary, so
    excerpts map to `highlights` and the first excerpt seeds `summary`. Parallel
    has no native date/domain filter, so those are applied client-side below —
    which means we must **over-fetch** when a filter is active, otherwise a
    domain- or date-restricted search would return far fewer than `num_results`
    (Parallel returns N mixed results, client-side filtering then drops most).
    """
    api_key = os.environ.get("PARALLEL_API_KEY")
    if not api_key:
        raise EnvironmentError("PARALLEL_API_KEY environment variable is not set.")

    import requests  # lazy: keeps provider resolution importable without it

    want = max(1, min(int(num_results), 40))  # Parallel caps max_results at 40
    # When a client-side filter is active, over-fetch enough that some survive
    # filtering — but a BOUNDED multiple, not the max-40 (Parallel bills by
    # results, so always buying 40 for a 3-result ask wasted credits).
    filters_active = bool(include_domains or exclude_domains) or days_back is not None
    fetch = min(40, max(want * 3, 10)) if filters_active else want

    # Parallel Search API: 'mode' replaced the old 'processor'; result/char
    # limits moved under 'advanced_settings'. Modes: turbo | basic | advanced.
    mode = (os.environ.get("SEARCH_PARALLEL_MODE")
            or os.environ.get("SEARCH_PARALLEL_PROCESSOR") or "base").strip().lower()
    if mode in ("base", "pro"):  # legacy processor names → nearest mode
        mode = {"base": "basic", "pro": "advanced"}[mode]
    if mode not in ("turbo", "basic", "advanced"):
        mode = "basic"
    body = {
        "objective": question or DEFAULT_SUMMARY_QUESTION,
        "search_queries": [query],
        "mode": mode,
        "advanced_settings": {
            "max_results": fetch,
            "excerpt_settings": {"max_chars_per_result": _PARALLEL_MAX_CHARS},
        },
    }

    # A burst of concurrent searches can trip Parallel's rate limit (429). That's
    # transient, so back off and retry a couple times before giving up — otherwise
    # one 429 wastes the call. Non-429 errors (incl. 402 credit exhaustion) fail
    # fast so the caller can latch the provider off and fall over.
    resp = None
    for attempt in range(3):
        _parallel_throttle()
        resp = requests.post(
            _PARALLEL_ENDPOINT,
            headers={"x-api-key": api_key, "Content-Type": "application/json"},
            json=body,
            timeout=90,  # 'advanced' mode can take 15-60s
        )
        if resp.status_code == 429 and attempt < 2:
            time.sleep(2 ** (attempt + 1))  # 2s, 4s
            continue
        break
    if resp.status_code != 200:
        raise RuntimeError(f"Parallel search HTTP {resp.status_code}: {resp.text[:200]}")

    data = resp.json()

    cutoff = None
    if days_back is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    inc = include_domains or []
    exc = exclude_domains or []

    out = []
    for r in data.get("results", []):
        url = r.get("url")
        if not url:
            continue
        host = (urlparse(url).netloc or "").lower()
        if inc and not any(_host_matches(host, d) for d in inc):
            continue
        if exc and any(_host_matches(host, d) for d in exc):
            continue

        pub = r.get("publish_date")
        if cutoff and pub:
            try:
                pd = datetime.fromisoformat(str(pub).replace("Z", "+00:00"))
                if pd.tzinfo is None:
                    pd = pd.replace(tzinfo=timezone.utc)
                if pd < cutoff:
                    continue
            except ValueError:
                pass  # unparseable date — keep the result rather than drop it

        excerpts = r.get("excerpts") or []
        out.append({
            "title": r.get("title"),
            "url": url,
            "published_date": pub,
            "author": None,  # Parallel does not return an author
            "summary": excerpts[0] if excerpts else "",
            "highlights": excerpts,
        })
        if len(out) >= want:  # honor num_results after client-side filtering
            break
    return out


def _dispatch(
    provider: str,
    query: str,
    num_results: int,
    days_back: Optional[int],
    include_domains: Optional[list],
    exclude_domains: Optional[list],
    question: str,
) -> List[dict]:
    fn = _search_exa if provider == "exa" else _search_parallel
    return fn(query, num_results, days_back, include_domains, exclude_domains, question)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def search_web(
    query: str,
    num_results: int = DEFAULT_NUM_RESULTS,
    days_back: Optional[int] = None,
    include_domains: Optional[list] = None,
    exclude_domains: Optional[list] = None,
    summary_question: Optional[str] = None,
) -> dict:
    """
    Fetches web results and structured insights for a query.

    Backed by Exa and/or Parallel (see SEARCH_PROVIDER above). Uses highlights /
    excerpts rather than full page text, keeping cost and token usage minimal.

    Args:
        query:            Search query (company name, person, topic)
        num_results:      Number of results (default 5, max 20 for Exa / 40 Parallel)
        days_back:        Only return results published in last N days
        include_domains:  Restrict to these domains
        exclude_domains:  Exclude these domains
        summary_question: Question answered per result from its content

    Returns:
        dict with keys: query, total, results, errors
    """
    # Hard cap: no caller can accidentally request a huge (billable) result set.
    # Exa is not otherwise clamped, so a bad num_results would go straight through.
    num_results = max(1, min(int(num_results), 40))
    mode, providers = _resolve_providers()
    question = summary_question or DEFAULT_SUMMARY_QUESTION
    print(f"Searching: {query!r} ({num_results} results) via {'+'.join(providers)}")

    if mode == "both":
        merged, errors = [], []
        for prov in providers:
            try:
                merged.extend(_dispatch(prov, query, num_results, days_back,
                                        include_domains, exclude_domains, question))
            except Exception as e:  # one backend failing must not lose the other's hits
                errors.append(f"{prov}: {e}")
                if _is_hard_fail(e):
                    _disable_provider(prov, e)
        merged = _dedup(merged)
        return {"query": query, "total": len(merged), "results": merged, "errors": errors}

    # single / fallback
    last_err = None
    for i, prov in enumerate(providers):
        try:
            results = _dedup(_dispatch(prov, query, num_results, days_back,
                                       include_domains, exclude_domains, question))
            return {"query": query, "total": len(results), "results": results, "errors": []}
        except Exception as e:
            last_err = e
            # A hard failure (exhausted credits, auth) means this provider is dead
            # for the run — latch it off so later searches skip it entirely.
            if _is_hard_fail(e):
                _disable_provider(prov, e)
            has_next = mode == "fallback" and i + 1 < len(providers)
            if has_next:
                print(f"  {prov} search failed ({e}); falling back to {providers[i + 1]}")
                continue
            raise
    raise last_err  # unreachable, but keeps the contract explicit


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
        # Isolate failures per query: one bad search (rate limit, timeout) must
        # not abort the whole batch, so return the standard error shape instead.
        try:
            return search_web(
                query=query,
                num_results=num_results,
                days_back=days_back,
                include_domains=include_domains,
                exclude_domains=exclude_domains,
                summary_question=summary_question,
            )
        except Exception as e:
            return {"query": query, "total": 0, "results": [], "errors": [str(e)]}

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
