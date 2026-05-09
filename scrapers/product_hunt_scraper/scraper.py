"""
Product Hunt Scraper — fetch recent product launches from Product Hunt.

No Apify, no API key required. Uses the free PH RSS feed.
Note: Product Hunt's Cloudflare protection blocks all datacenter IPs, so individual
product pages and topic-specific feeds are inaccessible. RSS only.

Input:  max_products (default 20), days_back (default 1)
Output: list of recent launches with name, tagline, author, date, ph_url
"""

import sys
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from typing import Optional, List

import requests

RSS_URL = "https://www.producthunt.com/feed"
ATOM_NS = "http://www.w3.org/2005/Atom"
PH_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"


def _parse_date(date_str: str) -> Optional[datetime]:
    try:
        # Handle both +00:00 and -07:00 style offsets
        return datetime.fromisoformat(date_str)
    except Exception:
        return None


def _parse_tagline(content_html: str) -> str:
    """Extract tagline from the HTML content field."""
    match = re.search(r"<p>\s*(.*?)\s*</p>", content_html, re.DOTALL)
    if match:
        return re.sub(r"<[^>]+>", "", match.group(1)).strip()
    return ""


def _parse_post_id(entry_id: str) -> str:
    """Extract numeric post ID from 'tag:www.producthunt.com,2005:Post/1120270'."""
    match = re.search(r"Post/(\d+)", entry_id)
    return match.group(1) if match else ""


def scrape_product_hunt(
    max_products: int = 20,
    days_back: int = 1,
) -> dict:
    """
    Fetch recent Product Hunt launches via RSS feed.

    Args:
        max_products: Max products to return. RSS feed has a hard cap of 50.
        days_back:    Only include products launched in the last N days. 0 = no filter.

    Returns:
        dict with keys: products, product_count, errors
    """
    errors = []

    try:
        resp = requests.get(RSS_URL, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        return {"products": [], "product_count": 0, "errors": [f"RSS fetch failed: {e}"]}

    try:
        root = ET.fromstring(resp.text)
    except Exception as e:
        return {"products": [], "product_count": 0, "errors": [f"RSS parse failed: {e}"]}

    ns = {"atom": ATOM_NS}
    entries = root.findall("atom:entry", ns)

    cutoff = None
    if days_back > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)

    products = []
    for entry in entries:
        published_str = entry.findtext("atom:published", "", ns)
        published_dt = _parse_date(published_str)

        if cutoff and published_dt and published_dt < cutoff:
            continue

        link_el = entry.find("atom:link", ns)
        ph_url = link_el.get("href", "") if link_el is not None else ""

        content_html = entry.findtext("atom:content", "", ns)
        tagline = _parse_tagline(content_html)
        post_id = _parse_post_id(entry.findtext("atom:id", "", ns))

        products.append({
            "name": entry.findtext("atom:title", "", ns),
            "tagline": tagline,
            "ph_url": ph_url,
            "post_id": post_id,
            "published_at": published_str,
            "submitter": entry.findtext("atom:author/atom:name", "", ns),
        })

    products = products[:max_products]

    return {
        "products": products,
        "product_count": len(products),
        "errors": errors,
    }


if __name__ == "__main__":
    input_file = sys.argv[1] if len(sys.argv) > 1 else "example_input.json"
    with open(input_file) as f:
        inp = json.load(f)

    result = scrape_product_hunt(
        max_products=inp.get("max_products", 20),
        days_back=inp.get("days_back", 1),
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
