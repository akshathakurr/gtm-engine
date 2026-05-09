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
from typing import List, Dict, Optional

try:
    from pytrends.request import TrendReq  # type: ignore
    _PYTRENDS_AVAILABLE = True
except Exception:
    TrendReq = None  # type: ignore
    _PYTRENDS_AVAILABLE = False


_BATCH_SIZE = 5  # Google Trends max
_SLEEP_BETWEEN_BATCHES = 1.0


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

    pytrends = TrendReq(hl="en-US", tz=0)
    out: Dict[str, Dict] = {}

    # Process in batches of 5 (Google Trends limit)
    for i in range(0, len(keywords), _BATCH_SIZE):
        batch = keywords[i:i + _BATCH_SIZE]
        batch_errors: Dict[str, List[str]] = {kw: [] for kw in batch}
        scores: Dict[str, int] = {kw: 0 for kw in batch}
        related: Dict[str, List[str]] = {kw: [] for kw in batch}
        rising:  Dict[str, List[str]] = {kw: [] for kw in batch}

        try:
            pytrends.build_payload(batch, cat=0, timeframe=timeframe, geo=geo, gprop="")
        except Exception as e:
            for kw in batch:
                batch_errors[kw].append(f"build_payload failed: {e}")
            for kw in batch:
                out[kw] = {
                    "interest_score": 0,
                    "related_queries": [],
                    "rising_queries":  [],
                    "errors": batch_errors[kw],
                }
            continue

        # Interest over time → average across the period
        try:
            df = pytrends.interest_over_time()
            if not df.empty:
                for kw in batch:
                    if kw in df.columns:
                        # mean is more stable than max; cast to int for sheet/CSV friendliness
                        scores[kw] = int(round(float(df[kw].mean())))
        except Exception as e:
            for kw in batch:
                batch_errors[kw].append(f"interest_over_time failed: {e}")

        # Related + rising queries
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
        except Exception as e:
            for kw in batch:
                batch_errors[kw].append(f"related_queries failed: {e}")

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
