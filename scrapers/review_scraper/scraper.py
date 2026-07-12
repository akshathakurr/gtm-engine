"""
Review Scraper — scrape product reviews from G2 and Trustpilot.

Platform dispatch: single entry point, routes to the correct backend per platform.

Platforms:
  g2          — zen-studio/g2-reviews-scraper (Apify, free plan, $0.003/review approx)
  trustpilot  — Jina Reader + regex parsing (free, no API key)
  capterra    — UNAVAILABLE: Cloudflare blocks all datacenter + Jina access

Input:  platform, product_url, max_reviews (default 20)
Output: dict with platform, product_url, overall_rating, total_review_count,
        reviews[], review_count, errors
"""

import os
import sys
import re
import json
from contextlib import contextmanager

import requests
from dotenv import load_dotenv

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
from scrapers._apify import dataset_items  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(__file__), "../../.env"))

JINA_BASE = "https://r.jina.ai/"
G2_ACTOR_ID = "zen-studio/g2-reviews-scraper"


# ── Apify log suppression ────────────────────────────────────────────────────

import logging

# Silence Apify's client logger once at import (thread-safe). Per-call actor-run
# log streaming is disabled via logger=None.
logging.getLogger("apify_client").setLevel(logging.WARNING)


@contextmanager
def _suppress_apify_logs():
    """No-op, kept for call-site compatibility.

    Previously swapped sys.stdout/sys.stderr to /dev/null, but a global stream
    swap corrupts output when this scraper runs alongside others on worker
    threads. Streaming is disabled at the source via ``.call(logger=None)``.
    """
    yield


# ── G2 ───────────────────────────────────────────────────────────────────────

def _scrape_g2(product_url: str, max_reviews: int) -> dict:
    from apify_client import ApifyClient

    api_key = os.environ.get("APIFY_API_TOKEN")
    if not api_key:
        return _error_result("g2", product_url, "APIFY_API_TOKEN not set")

    client = ApifyClient(api_key)
    # G2's actor bills a ~$0.036 per-run minimum (~13 reviews), so fetching fewer
    # than 13 costs the same as fetching 13 — always pull at least that many to
    # maximize data-per-dollar. We then RETURN all of them (not just the first
    # max_reviews): they're already paid for, so slicing them off would waste the
    # spend. The caller trims to what it needs.
    platform_max = max(max_reviews, 13)

    print(f"Scraping G2 reviews: {product_url}", file=sys.stderr)
    try:
        with _suppress_apify_logs():
            run = client.actor(G2_ACTOR_ID).call(
                run_input={"url": product_url},
                max_items=platform_max,
                logger=None,
            )
        raw_items = dataset_items(client, run)
    except Exception as e:
        return _error_result("g2", product_url, f"Actor run failed: {e}")

    reviews = []
    overall_rating = None  # G2 doesn't return an overall rating in items
    for item in raw_items[:platform_max]:
        reviews.append({
            "reviewer": item.get("reviewerName", ""),
            "reviewer_title": item.get("reviewerTitle", ""),
            "rating": item.get("starRating"),
            "title": item.get("title", ""),
            "text": item.get("text", ""),
            "date": item.get("date", ""),
            "verified": item.get("validatedReviewer", False),
            "incentivized": item.get("incentivized", False),
        })

    return {
        "platform": "g2",
        "product_url": product_url,
        "overall_rating": overall_rating,
        "total_review_count": None,  # not returned by actor
        "reviews": reviews,
        "review_count": len(reviews),
        "errors": [],
    }


# ── Trustpilot ───────────────────────────────────────────────────────────────

def _scrape_trustpilot(product_url: str, max_reviews: int) -> dict:
    print(f"Scraping Trustpilot reviews: {product_url}", file=sys.stderr)
    try:
        resp = requests.get(f"{JINA_BASE}{product_url}", timeout=20)
        resp.raise_for_status()
        text = resp.text
    except Exception as e:
        return _error_result("trustpilot", product_url, f"Jina fetch failed: {e}")

    # Overall rating and total count (two known Jina renderings of the page)
    rating_match = re.search(r"TrustScore ([\d.]+) out of 5", text) \
        or re.search(r"rated \"[^\"]+\" with ([\d.]+) / 5", text)
    overall_rating = float(rating_match.group(1)) if rating_match else None

    count_match = re.search(r"Reviews\s+(\d[\d,]+)", text)
    total_count = int(count_match.group(1).replace(",", "")) if count_match else None

    # AI summary
    summary_start = text.find("Based on reviews")
    summary_end = text.find("Was this summary helpful")
    summary = ""
    if summary_start != -1 and summary_end != -1:
        summary = re.sub(r"\s+", " ", text[summary_start:summary_end]).strip()

    # Individual reviews
    pattern = re.compile(
        r"Rated (\d) out of 5 stars\]\([^)]+\)\n\n(.*?)(?=Useful|Share)\n*Useful.*?\n\nShare\n\n\w+\n\n\[([^\]]+)\]",
        re.DOTALL,
    )
    reviews = []
    for m in pattern.finditer(text):
        if len(reviews) >= max_reviews:
            break
        rating_str, review_text, author_date = m.groups()

        # Split "Name Mon DD, YYYY"
        ad_match = re.match(r"(.+?)\s+((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d+,\s+\d{4})", author_date.strip())
        reviewer = ad_match.group(1).strip() if ad_match else author_date.strip()
        date = ad_match.group(2).strip() if ad_match else ""

        reviews.append({
            "reviewer": reviewer,
            "reviewer_title": "",
            "rating": int(rating_str),
            "title": "",
            "text": review_text.strip().replace("\n", " "),
            "date": date,
            "verified": False,
            "incentivized": False,
        })

    if not reviews:
        # Newer Jina rendering: "## [title](…/reviews/<id>)" blocks ending in a
        # bare "Month DD, YYYY" date line; no per-review star rating or reviewer
        # name is exposed in this format.
        block_pattern = re.compile(
            r"^## \[([^\]]+)\]\(https://www\.trustpilot\.com/reviews/[a-f0-9]+\)\n+"
            r"(.*?)\n+"
            r"((?:January|February|March|April|May|June|July|August|September|October|November|December)"
            r"\s+\d{1,2},\s+\d{4})\s*$",
            re.DOTALL | re.MULTILINE,
        )
        for m in block_pattern.finditer(text):
            if len(reviews) >= max_reviews:
                break
            title, review_text, date = m.groups()
            reviews.append({
                "reviewer": "",
                "reviewer_title": "",
                "rating": None,
                "title": title.strip(),
                "text": re.sub(r"\s+", " ", review_text).strip(),
                "date": date.strip(),
                "verified": False,
                "incentivized": False,
            })

    return {
        "platform": "trustpilot",
        "product_url": product_url,
        "overall_rating": overall_rating,
        "total_review_count": total_count,
        "ai_summary": summary,
        "reviews": reviews,
        "review_count": len(reviews),
        "errors": [],
    }


# ── Dispatch ─────────────────────────────────────────────────────────────────

def _error_result(platform: str, product_url: str, error: str) -> dict:
    return {
        "platform": platform,
        "product_url": product_url,
        "overall_rating": None,
        "total_review_count": None,
        "reviews": [],
        "review_count": 0,
        "errors": [error],
    }


SUPPORTED_PLATFORMS = {
    "g2": _scrape_g2,
    "trustpilot": _scrape_trustpilot,
}


def scrape_reviews(
    platform: str,
    product_url: str,
    max_reviews: int = 20,
) -> dict:
    """
    Scrape product reviews from a supported review platform.

    Args:
        platform:    "g2" or "trustpilot".
                     "capterra" is not supported — Cloudflare blocks all access.
        product_url: Direct URL to the product's review page.
                     G2: https://www.g2.com/products/{slug}/reviews
                     Trustpilot: https://www.trustpilot.com/review/{domain}
        max_reviews: Target number of reviews. G2 pulls (and returns) at least
                     ~13 regardless — the actor's per-run minimum charge covers
                     them, so they're returned rather than wasted.

    Returns:
        dict with: platform, product_url, overall_rating, total_review_count,
                   reviews[], review_count, errors
    """
    platform = platform.lower().strip()

    if platform == "capterra":
        return _error_result(
            "capterra", product_url,
            "Capterra is not supported: Cloudflare blocks all datacenter IPs and Jina Reader access."
        )

    if platform not in SUPPORTED_PLATFORMS:
        return _error_result(
            platform, product_url,
            f"Unknown platform '{platform}'. Supported: {list(SUPPORTED_PLATFORMS.keys())}"
        )

    return SUPPORTED_PLATFORMS[platform](product_url, max_reviews)


if __name__ == "__main__":
    input_file = sys.argv[1] if len(sys.argv) > 1 else "example_input.json"
    with open(input_file) as f:
        inp = json.load(f)

    result = scrape_reviews(
        platform=inp["platform"],
        product_url=inp["product_url"],
        max_reviews=inp.get("max_reviews", 20),
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
