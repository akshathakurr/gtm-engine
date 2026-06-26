"""
Firecrawl Scraper — fetch web pages as clean markdown, with JS rendering.

Firecrawl renders JavaScript and handles anti-bot/proxies, so it reliably
captures content that the static requests+BeautifulSoup scraper misses —
pricing tables, case-study indexes, and other JS-rendered sections.

Two entry points:
  - scrape_markdown(url)  -> str | None   : one page as markdown
  - scrape_website(url)   -> dict | None  : homepage + key subpages, returned in
                                            the same shape as website_scraper so
                                            it's a drop-in replacement.

Free plan: 1,000 credits/month (1 credit/page), 2 concurrent, low rate limit.
Needs FIRECRAWL_API_KEY. Both functions return None when the key is unset, so
callers can fall back to the static scraper and degrade gracefully.
"""

import os
import sys
import json
import time
from typing import Optional, Dict
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup

# Reuse the (fetch-agnostic) parsing helpers from the static scraper.
from scrapers.website_scraper import scraper as _ws

FIRECRAWL_SCRAPE_URL = "https://api.firecrawl.dev/v2/scrape"
DEFAULT_WAIT_MS = 3000      # let JS-rendered content settle before capture
DEFAULT_TIMEOUT = 60        # seconds for the HTTP request itself
MAX_KEY_PAGES = 5           # subpages beyond the homepage (bounds credits/latency)
FREE_PLAN_CONCURRENCY = 2   # Firecrawl free tier allows 2 concurrent requests
MAX_PAGE_CHARS = 12000      # cap markdown per page fed downstream (pricing tables sit deep in the page)
MAX_RETRIES = 3             # retry transient rate-limit (429) / 5xx errors
RETRY_BACKOFF = 5           # seconds; multiplied by attempt number


def _scrape(url: str, formats=None, wait_ms: int = DEFAULT_WAIT_MS,
            only_main_content: bool = True) -> Optional[dict]:
    """Low-level single-page scrape. Returns Firecrawl's `data` dict or None.

    Retries on 429 / 5xx with linear backoff — the free tier has a low rate
    limit, and a throttled call must not surface as silently-empty data.
    """
    api_key = os.environ.get("FIRECRAWL_API_KEY")
    if not api_key:
        return None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(
                FIRECRAWL_SCRAPE_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "url": url,
                    "formats": formats or ["markdown"],
                    "onlyMainContent": only_main_content,
                    "waitFor": wait_ms,
                },
                timeout=DEFAULT_TIMEOUT + wait_ms // 1000,
            )
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF * attempt)
                continue
            resp.raise_for_status()
            data = resp.json()
            if not data.get("success"):
                return None
            return data.get("data") or None
        except Exception:
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF * attempt)
                continue
            return None
    return None


def scrape_markdown(
    url: str,
    wait_ms: int = DEFAULT_WAIT_MS,
    only_main_content: bool = True,
) -> Optional[str]:
    """
    Scrape a single URL and return its main content as markdown.

    Returns None if FIRECRAWL_API_KEY is unset, the call fails, or no markdown
    came back — callers should treat None as "keep what you had".
    """
    data = _scrape(url, formats=["markdown"], wait_ms=wait_ms,
                   only_main_content=only_main_content)
    if not data:
        return None
    return data.get("markdown") or None


def _empty_result(base_url: str, error: str) -> dict:
    return {
        "url": base_url, "company_name": None, "meta_description": None,
        "homepage_text": None, "pages_scraped": [], "product_description": None,
        "customers": [], "employee_size_hint": None, "founded_year": None,
        "icp_hints": [], "full_text_by_page": {}, "page_urls": {},
        "errors": [error],
    }


def scrape_website(url: str, max_pages: int = MAX_KEY_PAGES) -> Optional[dict]:
    """
    Scrape a company website via Firecrawl (homepage + key subpages) and return
    structured info in the SAME shape as website_scraper.scrape_website().

    Returns None when FIRECRAWL_API_KEY is unset, so the caller can fall back to
    the static scraper.
    """
    if not os.environ.get("FIRECRAWL_API_KEY"):
        return None

    if not url.startswith("http"):
        url = "https://" + url
    base_url = url.rstrip("/")

    print(f"[Firecrawl] Fetching homepage: {base_url}")
    home = _scrape(base_url, formats=["markdown", "html"])
    if not home or not (home.get("markdown") or home.get("html")):
        return _empty_result(base_url, f"Firecrawl failed to fetch homepage: {base_url}")

    home_html = home.get("html") or ""
    home_soup = BeautifulSoup(home_html, "html.parser") if home_html else BeautifulSoup("", "html.parser")
    homepage_md = (home.get("markdown") or "")[:MAX_PAGE_CHARS]

    meta = _ws._extract_meta(home_soup)
    # Markdown is cleaner than soup text; fall back to soup text if markdown empty.
    homepage_text = homepage_md or _ws._extract_page_text(home_soup)

    # Discover key subpages from the homepage links (reuses static discovery).
    key_page_urls = _ws._discover_key_pages(home_soup, base_url)

    # Guarantee a pricing attempt: if discovery found no pricing/plans page,
    # guess the conventional path (cheap, high-value — pricing fails most often).
    if not any(p in u.lower() for u in key_page_urls for p in ("pricing", "plans")):
        key_page_urls.append(base_url + "/pricing")
    key_page_urls = key_page_urls[:max_pages]

    print(f"[Firecrawl] Fetching {len(key_page_urls)} subpage(s): {key_page_urls}")

    full_text_by_page = {"homepage": homepage_text}
    page_urls = {"homepage": base_url}
    pages_scraped = ["homepage"]
    all_soups = [home_soup]
    errors = []

    def _fetch(u):
        return u, _scrape(u, formats=["markdown", "html"])

    # Respect the free-tier 2-concurrent limit.
    with ThreadPoolExecutor(max_workers=FREE_PLAN_CONCURRENCY) as pool:
        futures = {pool.submit(_fetch, u): u for u in key_page_urls}
        for fut in as_completed(futures):
            page_url, data = fut.result()
            if not data or not data.get("markdown"):
                errors.append(f"Firecrawl failed/empty: {page_url}")
                continue
            label = urlparse(page_url).path.strip("/").replace("/", "_") or "home"
            full_text_by_page[label] = (data.get("markdown") or "")[:MAX_PAGE_CHARS]
            page_urls[label] = page_url
            pages_scraped.append(label)
            if data.get("html"):
                all_soups.append(BeautifulSoup(data["html"], "html.parser"))

    # Cross-page extraction — reuse the static scraper's heuristics on the text.
    all_text = " ".join(full_text_by_page.values())
    text_customers = _ws._extract_customer_names(all_text)
    logo_customers = []
    for soup in all_soups:
        logo_customers.extend(_ws._extract_customers_from_images(soup))
    customers = list(dict.fromkeys(logo_customers + text_customers))

    product_description = meta["meta_description"] or homepage_text[:500]

    return {
        "url": base_url,
        "company_name": meta["title"],
        "meta_description": meta["meta_description"],
        "homepage_text": homepage_text,
        "pages_scraped": pages_scraped,
        "product_description": product_description,
        "customers": customers,
        "employee_size_hint": _ws._extract_employee_size_hint(all_text),
        "founded_year": _ws._extract_founded_year(all_text),
        "icp_hints": _ws._extract_icp_hints(all_text),
        "full_text_by_page": full_text_by_page,
        "page_urls": page_urls,
        "errors": errors,
    }


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

    if inp.get("mode") == "website":
        result = scrape_website(inp["url"])
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        md = scrape_markdown(
            url=inp["url"],
            wait_ms=inp.get("wait_ms", DEFAULT_WAIT_MS),
            only_main_content=inp.get("only_main_content", True),
        )
        print(json.dumps({"url": inp["url"], "markdown": md, "chars": len(md or "")}, indent=2, ensure_ascii=False))
