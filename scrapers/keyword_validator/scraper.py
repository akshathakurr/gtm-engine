"""
Keyword Validator — sanity-check SEO keywords using Google Trends.

Backed by `pytrends` (free, no auth). Returns a relative interest score (0-100)
plus related and rising queries for each keyword. The score is *relative*, not
absolute volume — it's enough to distinguish "real searches happen here" from
"nobody searches this" but not for absolute traffic estimates.

If you want absolute search volume later, swap the implementation here for
DataForSEO / SerpAPI / Google Ads Keyword Planner — all callers see the same
{keyword: {interest_score, related, rising}} shape.

Cost: free.
Auth: none.
Rate limits: pytrends is unofficial; aggressive use can get IPs throttled.
            We sleep 1s between batches and cap at 5 keywords per request.
"""

import os
import sys
import json
import time
from typing import List, Dict

try:
    from pytrends.request import TrendReq  # type: ignore
    _PYTRENDS_AVAILABLE = True
except Exception:
    TrendReq = None  # type: ignore
    _PYTRENDS_AVAILABLE = False


_BATCH_SIZE = 5  # Google Trends max
_SLEEP_BETWEEN_BATCHES = 1.0

# Google Trends 429s the default pytrends User-Agent from datacenter IPs; a real
# browser UA is what gets it to answer. See _new_client for the urllib3 caveat.
_BROWSER_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
_MAX_RETRIES = 4  # transient 429/5xx from Google Trends


def _new_client(hl: str = "en-US", tz: int = 360):
    """Build a pytrends client with a browser User-Agent.

    We deliberately do NOT pass ``retries=``/``backoff_factor=``: on urllib3 2.x
    pytrends builds its Retry with the removed ``method_whitelist`` kwarg and
    raises ``TypeError``. We run our own backoff loop instead (see below).
    """
    return TrendReq(hl=hl, tz=tz, timeout=(10, 25),
                    requests_args={"headers": _BROWSER_UA})


def validate_keywords(
    keywords: List[str],
    geo: str = "",
    timeframe: str = "today 12-m",
) -> Dict[str, Dict]:
    """
    Score each keyword by Google Trends interest and pull related queries.

    Args:
        keywords:  list of phrases to validate.
        geo:       country code (e.g. "US", "IN") or "" for worldwide.
        timeframe: pytrends timeframe — "today 12-m", "today 5-y", etc.

    Returns:
        {
          keyword: {
            "interest_score": int,         # 0-100, peak average over timeframe
            "related_queries": [str, ...], # up to 10 most-related queries
            "rising_queries":  [str, ...], # up to 10 fastest-growing related queries
            "errors": [str, ...]           # populated only on per-keyword failure
          }
        }

    If pytrends is not installed, returns {kw: {"errors": ["pytrends not installed"]}}
    for every keyword without raising.
    """
    if not _PYTRENDS_AVAILABLE:
        return {kw: {"interest_score": 0, "related_queries": [], "rising_queries": [],
                     "errors": ["pytrends not installed — pip install pytrends"]}
                for kw in keywords}

    if not keywords:
        return {}

    out: Dict[str, Dict] = {}

    # Process in batches of 5 (Google Trends limit)
    for i in range(0, len(keywords), _BATCH_SIZE):
        batch = keywords[i:i + _BATCH_SIZE]
        batch_errors: Dict[str, List[str]] = {kw: [] for kw in batch}
        scores: Dict[str, int] = {kw: 0 for kw in batch}
        related: Dict[str, List[str]] = {kw: [] for kw in batch}
        rising:  Dict[str, List[str]] = {kw: [] for kw in batch}

        # PRIMARY: payload + interest_over_time (the score callers actually use)
        # with our own exponential backoff on a fresh browser-UA client each try.
        # Google occasionally 429s even a browser UA; a fresh client + short wait
        # usually clears it. Only recorded as an error after all retries fail.
        pytrends = None
        last_err = None
        for attempt in range(_MAX_RETRIES):
            try:
                pytrends = _new_client(tz=0 if geo else 360)
                pytrends.build_payload(batch, cat=0, timeframe=timeframe, geo=geo, gprop="")
                df = pytrends.interest_over_time()
                if not df.empty:
                    for kw in batch:
                        if kw in df.columns:
                            # mean is more stable than max; int for sheet/CSV friendliness
                            scores[kw] = int(round(float(df[kw].mean())))
                last_err = None
                break
            except Exception as e:
                last_err = e
                pytrends = None
                if attempt < _MAX_RETRIES - 1:
                    time.sleep(2 ** attempt)  # 1s, 2s, 4s

        if last_err is not None:
            for kw in batch:
                batch_errors[kw].append(f"Google Trends failed after {_MAX_RETRIES} tries: {last_err}")

        # OPTIONAL: related/rising queries. This endpoint is throttled far more
        # aggressively than interest_over_time, so it's strictly best-effort —
        # if it 429s we keep the score and just leave related/rising empty
        # (no error, since the useful signal already landed above).
        if pytrends is not None:
            for attempt in range(_MAX_RETRIES):
                try:
                    rq = pytrends.related_queries()
                    for kw in batch:
                        rq_kw = rq.get(kw) or {}
                        top = rq_kw.get("top")
                        rs  = rq_kw.get("rising")
                        if top is not None and not top.empty:
                            related[kw] = [str(q) for q in top["query"].head(10).tolist()]
                        if rs is not None and not rs.empty:
                            rising[kw] = [str(q) for q in rs["query"].head(10).tolist()]
                    break
                except Exception:
                    if attempt < _MAX_RETRIES - 1:
                        time.sleep(2 ** attempt)

        for kw in batch:
            out[kw] = {
                "interest_score": scores[kw],
                "related_queries": related[kw],
                "rising_queries":  rising[kw],
                "errors": batch_errors[kw],
            }

        if i + _BATCH_SIZE < len(keywords):
            time.sleep(_SLEEP_BETWEEN_BATCHES)

    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    input_file = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(__file__), "example_input.json"
    )
    with open(input_file) as f:
        inp = json.load(f)

    result = validate_keywords(
        keywords=inp["keywords"],
        geo=inp.get("geo", ""),
        timeframe=inp.get("timeframe", "today 12-m"),
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
