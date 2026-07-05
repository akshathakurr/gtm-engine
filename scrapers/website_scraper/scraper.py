"""
Website Scraper — extract structured company info from a website.

Uses requests + BeautifulSoup4 for static sites.
Falls back to Jina Reader (r.jina.ai) for JS-heavy sites — free, no API key.
Fetches homepage + key subpages (about, product, pricing, careers, customers).
"""

import sys
import json
import re
from datetime import datetime
from typing import Optional, List, Dict
from urllib.parse import urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup

# Pages we actively try to discover and scrape
KEY_PAGE_PATTERNS = [
    "about", "about-us", "company",
    "product", "products", "platform", "solution", "solutions", "features",
    "pricing", "plans",
    "customers", "case-studies", "case_studies", "stories", "clients",
    "careers", "jobs", "work-with-us", "join-us", "team",
]

# Direct paths to try when nav discovery finds fewer than this many pages
DIRECT_PATH_FALLBACK_THRESHOLD = 2
DIRECT_PATHS = [
    "/about", "/about-us", "/company",
    "/product", "/platform", "/solutions", "/features",
    "/pricing", "/plans",
    "/customers", "/case-studies", "/customer-stories",
    "/careers", "/jobs",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

REQUEST_TIMEOUT = 15
MAX_KEY_PAGES = 6  # cap to avoid over-fetching
JINA_THIN_THRESHOLD = 200  # chars — below this, treat direct fetch as failed and try Jina
JINA_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "text/plain"}


def _fetch_via_jina(url: str) -> Optional[str]:
    """Fetch a URL via Jina Reader (handles JS-rendered sites). Returns plain text or None."""
    try:
        resp = requests.get(f"https://r.jina.ai/{url}", headers=JINA_HEADERS, timeout=30)
        resp.raise_for_status()
        return resp.text
    except Exception:
        return None


def _fetch_page(url: str) -> Optional[BeautifulSoup]:
    """
    Fetch a URL and return a BeautifulSoup object.
    Falls back to Jina Reader if direct fetch fails or returns thin content.
    """
    soup = None
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        resp.raise_for_status()
        ct = resp.headers.get("Content-Type", "")
        if "html" in ct:
            soup = BeautifulSoup(resp.text, "html.parser")
    except Exception:
        pass

    # Check if we got useful content
    if soup is not None:
        text_preview = soup.get_text(separator=" ").strip()
        if len(text_preview) >= JINA_THIN_THRESHOLD:
            return soup

    # Fallback: Jina Reader
    print(f"  Direct fetch thin/failed, trying Jina Reader for: {url}")
    jina_text = _fetch_via_jina(url)
    if jina_text:
        return BeautifulSoup(f"<pre>{jina_text}</pre>", "html.parser")

    return None


def _clean_text(text: str) -> str:
    """Strip excess whitespace."""
    return re.sub(r"\s+", " ", text).strip()


def _extract_page_text(soup: BeautifulSoup, max_chars: int = 3000) -> str:
    """Extract readable text from a page, stripping scripts/styles."""
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        tag.decompose()
    text = soup.get_text(separator=" ")
    return _clean_text(text)[:max_chars]


def _extract_meta(soup: BeautifulSoup) -> Dict[str, str]:
    """Pull title and meta description."""
    title = ""
    if soup.title and soup.title.string:
        title = _clean_text(soup.title.string)

    description = ""
    for attr in [{"name": "description"}, {"property": "og:description"}]:
        tag = soup.find("meta", attrs=attr)
        if tag and tag.get("content"):
            description = _clean_text(tag["content"])
            break

    og_title = ""
    og_tag = soup.find("meta", property="og:title")
    if og_tag and og_tag.get("content"):
        og_title = _clean_text(og_tag["content"])

    return {
        "title": og_title or title,
        "meta_description": description,
    }


def _discover_key_pages(soup: BeautifulSoup, base_url: str) -> List[str]:
    """
    Scan all <a href> links on the page and collect internal URLs
    whose path matches KEY_PAGE_PATTERNS. Deduplicated and capped.
    Falls back to trying common direct paths if nav discovery finds too few.
    """
    base_domain = urlparse(base_url).netloc
    found = {}

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        full_url = urljoin(base_url, href)
        parsed = urlparse(full_url)

        if parsed.netloc != base_domain:
            continue
        if parsed.fragment:
            continue

        path = parsed.path.rstrip("/").lower()
        path_parts = [p for p in path.split("/") if p]

        for pattern in KEY_PAGE_PATTERNS:
            if any(pattern in part for part in path_parts):
                clean_url = full_url.split("?")[0].split("#")[0]
                if clean_url not in found and clean_url != base_url.rstrip("/"):
                    found[pattern] = clean_url
                break

    discovered = list(found.values())

    # Direct path fallback — try common paths if nav discovery was sparse
    if len(discovered) < DIRECT_PATH_FALLBACK_THRESHOLD:
        for path in DIRECT_PATHS:
            candidate = base_url.rstrip("/") + path
            if candidate in discovered:
                continue
            try:
                r = requests.head(candidate, headers=HEADERS, timeout=8, allow_redirects=True)
                if r.status_code == 200:
                    discovered.append(candidate)
                    if len(discovered) >= MAX_KEY_PAGES:
                        break
            except Exception:
                continue

    return discovered[:MAX_KEY_PAGES]


def _extract_customers_from_images(soup: BeautifulSoup) -> List[str]:
    """
    Extract customer/partner names from <img> alt text and src filenames.
    Covers logo walls where company names are never in visible text.

    Strategy:
    - alt text: filter noise words, keep short proper-noun strings
    - src filename: extract name segment from patterns like 'logo-confluent.svg'
      or 'mob-8x-logo-shein.svg'
    """
    NOISE_ALTS = {
        "logo", "icon", "image", "photo", "avatar", "banner", "bg",
        "background", "pattern", "placeholder", "featured", "play",
        "arrow", "check", "star", "quote", "close", "menu", "search",
        "portrait", "headshot", "thumbnail", "badge", "seal", "award",
    }
    names = set()

    for img in soup.find_all("img"):
        # --- alt text ---
        alt = (img.get("alt") or "").strip().rstrip(".")
        if alt and 2 <= len(alt) <= 50:
            # Strip trailing " Logo", " logo", " Icon" etc.
            clean = re.sub(r"\s*(logo|icon|logotype|logomark|white|black|dark|light|color)\s*$", "", alt, flags=re.IGNORECASE).strip()
            lower = clean.lower()
            # Skip if it's a noise word or looks like a person name / UI label
            if (clean and
                not any(n in lower for n in NOISE_ALTS) and
                not re.search(r"\b(portrait|potrait|pattern|overview|featured|story|stories)\b", lower) and
                re.match(r"^[A-Z]", clean)):
                names.add(clean)

        # --- src filename ---
        src = img.get("src") or img.get("data-src") or ""
        filename = src.split("/")[-1].split("?")[0].split(".")[0]  # e.g. "mob-8x-logo-shein"
        # Look for "logo-<name>" or "logo_<name>" pattern
        m = re.search(r"logo[-_]([a-z0-9]+(?:[-_][a-z0-9]+)*)", filename, re.IGNORECASE)
        if m:
            raw = m.group(1).replace("-", " ").replace("_", " ").title()
            if 2 <= len(raw) <= 40 and raw.lower() not in NOISE_ALTS:
                names.add(raw)

    # Final filter: remove obvious UI strings and multi-word noise
    UI_NOISE = {"Overview", "Platform", "Product", "Solution", "Feature",
                "Pricing", "About", "Home", "Nav", "Header", "Footer", "Cta",
                "Hero", "Section", "Card", "Button", "Link", "Page", "Billing",
                "Payments", "Spend", "Accounts", "Business Accounts", "Platform APIs"}
    results = []
    for n in names:
        if n in UI_NOISE:
            continue
        # Skip strings that look like UI labels (contain "Tab", "Mobile", "Hero", "Asset", "Blot", "Bolt")
        if re.search(r'\b(Tab|Mobile|Hero|Asset|Blot|Bolt|Bento|Clean|Black|Blue|Coloured|Updated|Content|Decorative|Graph|Preview|Only|General|World Wide Web)\b', n):
            continue
        # Skip sentences (contain lowercase words — proper company names are title-cased)
        if re.search(r'\b[a-z]{4,}\b', n):
            continue
        # Skip person names (Firstname Lastname pattern where both are capitalized single words)
        words = n.split()
        if len(words) == 2 and all(re.match(r'^[A-Z][a-z]+$', w) for w in words):
            # Likely a person name — skip unless it's a known company format
            continue
        results.append(n)
    return sorted(dict.fromkeys(results))


def _extract_customer_names(text: str) -> List[str]:
    """
    Heuristic: look for "trusted by", "customers include", "used by" patterns
    followed by a list of proper nouns.
    """
    customers = []
    patterns = [
        r"(?:trusted by|used by|customers include|our customers|loved by|join|companies like)[:\s]+([^.]{5,200})",
        r"(?:case studies?|success stories?)[:\s]+([^.]{5,200})",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            raw = m.group(1)
            parts = re.split(r",|and|&|\n", raw)
            for p in parts:
                name = _clean_text(p)
                if 2 <= len(name) <= 40 and not re.search(r"\b(the|for|from|with|our|your)\b", name, re.IGNORECASE):
                    customers.append(name)
    return list(dict.fromkeys(customers))[:10]


def _extract_employee_size_hint(text: str) -> Optional[str]:
    """Look for headcount or employee count mentions."""
    patterns = [
        r"(\d[\d,]+)\s*(?:employees|people|team members|professionals|staff)",
        r"(?:team of|workforce of|over|more than)\s*(\d[\d,]+)",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(1).replace(",", "")
    return None


def _extract_founded_year(text: str) -> Optional[str]:
    """Look for founding year mention."""
    m = re.search(r"(?:founded|established|started|since)\s+(?:in\s+)?(\d{4})", text, re.IGNORECASE)
    if m:
        year = int(m.group(1))
        if 1900 <= year <= datetime.now().year:
            return str(year)
    return None


def _extract_icp_hints(text: str) -> List[str]:
    """
    Look for target customer signals: "for X teams", "designed for", "built for", etc.
    """
    hints = []
    patterns = [
        r"(?:built for|designed for|made for|for|helping)\s+([a-zA-Z\s,&-]{5,60}?)(?:\.|,|to |who )",
        r"(?:teams|companies|businesses|organizations|startups|enterprises)\s+(?:that|who|looking to|trying to)\s+([^.]{5,80})",
    ]
    for pat in patterns:
        for m in re.finditer(pat, text, re.IGNORECASE):
            hint = _clean_text(m.group(1))
            if len(hint) > 4:
                hints.append(hint)
    return list(dict.fromkeys(hints))[:5]


def scrape_website(url: str) -> dict:
    """
    Scrape a company website and return structured information.

    Args:
        url: Homepage URL (e.g. "https://acko.com")

    Returns:
        dict with keys: url, company_name, meta_description, homepage_text,
        pages_scraped, product_description, customers, employee_size_hint,
        founded_year, icp_hints, full_text_by_page, errors
    """
    errors = []

    # Normalize URL
    if not url.startswith("http"):
        url = "https://" + url
    base_url = url.rstrip("/")

    print(f"Fetching homepage: {base_url}")
    homepage_soup = _fetch_page(base_url)
    if homepage_soup is None:
        return {
            "url": base_url,
            "company_name": None,
            "meta_description": None,
            "homepage_text": None,
            "pages_scraped": [],
            "product_description": None,
            "customers": [],
            "employee_size_hint": None,
            "founded_year": None,
            "icp_hints": [],
            "full_text_by_page": {},
            "page_urls": {},
            "errors": [f"Failed to fetch homepage: {base_url}"],
        }

    meta = _extract_meta(homepage_soup)
    homepage_text = _extract_page_text(homepage_soup)

    # Discover key subpages
    key_page_urls = _discover_key_pages(homepage_soup, base_url)
    print(f"Discovered {len(key_page_urls)} key pages: {key_page_urls}")

    # Fetch key pages in parallel — keep soups for logo extraction
    full_text_by_page = {"homepage": homepage_text}
    page_urls = {"homepage": base_url}  # label -> source URL, for downstream re-fetch
    pages_scraped = ["homepage"]
    all_soups = [homepage_soup]

    def _fetch_and_extract(page_url):
        soup = _fetch_page(page_url)
        if soup is None:
            return page_url, None, None
        return page_url, _extract_page_text(soup), soup

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_fetch_and_extract, u): u for u in key_page_urls}
        for future in as_completed(futures):
            page_url, text, soup = future.result()
            label = urlparse(page_url).path.strip("/").replace("/", "_") or "home"
            if text:
                full_text_by_page[label] = text
                page_urls[label] = page_url
                pages_scraped.append(label)
                all_soups.append(soup)
            else:
                errors.append(f"Failed to fetch: {page_url}")

    # Combine all text for cross-page extraction
    all_text = " ".join(full_text_by_page.values())

    # Extract customers from text patterns + logo images across all pages
    text_customers = _extract_customer_names(all_text)
    logo_customers = []
    for soup in all_soups:
        logo_customers.extend(_extract_customers_from_images(soup))
    # Merge: logo extraction is more reliable, put it first
    customers = list(dict.fromkeys(logo_customers + text_customers))

    employee_size_hint = _extract_employee_size_hint(all_text)
    founded_year = _extract_founded_year(all_text)
    icp_hints = _extract_icp_hints(all_text)

    # Product description: prefer meta description, fallback to first 500 chars of homepage
    product_description = meta["meta_description"] or homepage_text[:500]

    return {
        "url": base_url,
        "company_name": meta["title"],
        "meta_description": meta["meta_description"],
        "homepage_text": homepage_text,
        "pages_scraped": pages_scraped,
        "product_description": product_description,
        "customers": customers,
        "employee_size_hint": employee_size_hint,
        "founded_year": founded_year,
        "icp_hints": icp_hints,
        "full_text_by_page": full_text_by_page,
        "page_urls": page_urls,
        "errors": errors,
    }


if __name__ == "__main__":
    input_file = sys.argv[1] if len(sys.argv) > 1 else "example_input.json"
    with open(input_file) as f:
        inp = json.load(f)

    result = scrape_website(url=inp["url"])
    print(json.dumps(result, indent=2, ensure_ascii=False))
