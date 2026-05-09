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
from typing import Optional, List
from contextlib import contextmanager

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "../../.env"))

JINA_BASE = "https://r.jina.ai/"
G2_ACTOR_ID = "zen-studio/g2-reviews-scraper"


# ── Apify log suppression ────────────────────────────────────────────────────

@contextmanager
def _suppress_apify_logs():
    with open(os.devnull, "w") as devnull:
        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout = devnull
        sys.stderr = devnull
        try:
            yield
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr


# ── G2 ───────────────────────────────────────────────────────────────────────

def _scrape_g2(product_url: str, max_reviews: int) -> dict:
    from apify_client import ApifyClient

    api_key = os.environ.get("APIFY_API_TOKEN")
    if not api_key:
        return _error_result("g2", product_url, "APIFY_API_TOKEN not set")

    client = ApifyClient(api_key)
    platform_max = max(max_reviews, 13)

    print(f"Scraping G2 reviews: {product_url}", file=sys.stderr)
    try:
        with _suppress_apify_logs():
            run = client.actor(G2_ACTOR_ID).call(
                run_input={"url": product_url},
                max_items=platform_max,
            )
        raw_items = list(client.dataset(run["defaultDatasetId"]).iterate_items())
    except Exception as e:
        return _error_result("g2", product_url, f"Actor run failed: {e}")

    reviews = []
    overall_rating = None
    for item in raw_items[:max_reviews]:
        if not overall_rating and item.get("starRating"):
            pass  # G2 doesn't return overall rating in items

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

    # Overall rating and total count
    rating_match = re.search(r"TrustScore ([\d.]+) out of 5", text)
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
        max_reviews: Max reviews to return.

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
