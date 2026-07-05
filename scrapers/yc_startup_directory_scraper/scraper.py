"""
YC Startup Directory Scraper — extract startup data from the Y Combinator directory.

No Apify, no API key required.
- Company list: YC public API (api.ycombinator.com/v0.1/companies)
- Founder details: Jina Reader on individual company pages (optional)

Input:  batch, industries, locations, status, max_companies, include_founders
Output: list of companies with name, description, website, batch, industries,
        locations, tags, team_size, is_hiring, yc_url, founders (if requested)
"""

import sys
import json
import re
from typing import Optional, List
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

YC_API_BASE = "https://api.ycombinator.com/v0.1/companies"
JINA_BASE = "https://r.jina.ai/"
PAGE_SIZE = 100  # Max per page from YC API


def _fetch_companies_page(batch: Optional[str], page: int) -> dict:
    params = {"count": PAGE_SIZE, "page": page}
    if batch:
        params["batch"] = batch
    resp = requests.get(YC_API_BASE, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _fetch_all_companies(batch: Optional[str]) -> List[dict]:
    """Fetch all companies for a batch (or all batches) via pagination."""
    first = _fetch_companies_page(batch, 1)
    companies = first.get("companies", [])
    total_pages = first.get("totalPages", 1)

    for page in range(2, total_pages + 1):
        data = _fetch_companies_page(batch, page)
        companies.extend(data.get("companies", []))

    return companies


def _parse_company(raw: dict) -> dict:
    badges = raw.get("badges", [])
    return {
        "name": raw.get("name", ""),
        "slug": raw.get("slug", ""),
        "one_liner": raw.get("oneLiner", ""),
        "description": raw.get("longDescription", ""),
        "website": raw.get("website", ""),
        "yc_url": raw.get("url", ""),
        "batch": raw.get("batch", ""),
        "status": raw.get("status", ""),
        "industries": raw.get("industries", []),
        "tags": raw.get("tags", []),
        "locations": raw.get("locations", []),
        "regions": raw.get("regions", []),
        "team_size": raw.get("teamSize", 0),
        "is_hiring": "isHiring" in badges,
        "founders": [],
    }


def _fetch_founders(slug: str) -> List[dict]:
    """Scrape founder details from a YC company page via Jina Reader."""
    try:
        url = f"{JINA_BASE}https://www.ycombinator.com/companies/{slug}"
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        text = resp.text

        start = text.find("Active Founders")
        if start == -1:
            return []

        section = text[start:start + 8000]

        # Each founder block: image alt name → name line → linkedin → "Founder" → bio
        # Pattern matches name from image alt text, then linkedin URL, then bio
        pattern = re.compile(
            r'!\[Image \d+: ([^\]]+)\]\([^)]+\)\n\n'  # image with name in alt
            r'\1\n\n'                                    # name repeated
            r'\[\]\(([^)]*)\)\n\n'                       # linkedin url (empty text link)
            r'Founder\n\n'                               # role label
            r'(.*?)(?=!\[Image|\Z)',                     # bio until next image or end
            re.DOTALL
        )

        seen = set()
        founders = []
        for m in pattern.finditer(section):
            name = m.group(1).strip()
            if name in seen:
                continue
            seen.add(name)
            linkedin = m.group(2).strip()
            bio = m.group(3).strip()
            founders.append({
                "name": name,
                "linkedin_url": linkedin,
                "bio": bio,
            })

        return founders
    except Exception:
        return []


def scrape_yc_companies(
    batch: Optional[str] = None,
    industries: Optional[List[str]] = None,
    locations: Optional[List[str]] = None,
    status: Optional[str] = None,
    max_companies: int = 50,
    include_founders: bool = False,
) -> dict:
    """
    Scrape YC startup directory.

    Args:
        batch:            YC batch code e.g. "W24", "S23". None = all batches.
        industries:       Filter by industry (client-side). e.g. ["B2B", "Healthcare"].
                          Partial match, case-insensitive. None = no filter.
        locations:        Filter by location string (client-side). e.g. ["San Francisco", "New York"].
                          Partial match, case-insensitive. None = no filter.
        status:           Filter by status: "Active", "Inactive", "Acquired", "Public". None = all.
        max_companies:    Max companies to return.
        include_founders: If True, fetches founder details for each company via Jina.
                          Slower (parallel HTTP calls), but adds name, linkedin, bio.

    Returns:
        dict with keys: batch, filters, companies, company_count, errors
    """
    errors = []

    print(f"Fetching YC companies (batch={batch or 'all'})...", file=sys.stderr)
    try:
        raw_companies = _fetch_all_companies(batch)
    except Exception as e:
        return {
            "batch": batch,
            "filters": {"industries": industries, "locations": locations, "status": status},
            "companies": [],
            "company_count": 0,
            "errors": [f"API fetch failed: {e}"],
        }

    print(f"  Fetched {len(raw_companies)} raw companies", file=sys.stderr)

    # Parse
    companies = [_parse_company(c) for c in raw_companies]

    # Client-side filtering
    if status:
        companies = [c for c in companies if c["status"].lower() == status.lower()]

    if industries:
        def _matches_industry(c: dict) -> bool:
            company_industries = " ".join(c["industries"] + c["tags"]).lower()
            return any(ind.lower() in company_industries for ind in industries)
        companies = [c for c in companies if _matches_industry(c)]

    if locations:
        def _matches_location(c: dict) -> bool:
            company_locs = " ".join(c["locations"] + c["regions"]).lower()
            return any(loc.lower() in company_locs for loc in locations)
        companies = [c for c in companies if _matches_location(c)]

    companies = companies[:max_companies]

    # Fetch founders in parallel if requested
    if include_founders and companies:
        print(f"  Fetching founders for {len(companies)} companies (parallel)...", file=sys.stderr)
        slugs = [c["slug"] for c in companies]
        slug_to_index = {c["slug"]: i for i, c in enumerate(companies)}

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(_fetch_founders, slug): slug for slug in slugs}
            for future in as_completed(futures):
                slug = futures[future]
                try:
                    founders = future.result()
                    companies[slug_to_index[slug]]["founders"] = founders
                except Exception as e:
                    errors.append(f"Founder fetch failed for {slug}: {e}")

    return {
        "batch": batch,
        "filters": {"industries": industries, "locations": locations, "status": status},
        "companies": companies,
        "company_count": len(companies),
        "errors": errors,
    }


if __name__ == "__main__":
    input_file = sys.argv[1] if len(sys.argv) > 1 else "example_input.json"
    with open(input_file) as f:
        inp = json.load(f)

    result = scrape_yc_companies(
        batch=inp.get("batch"),
        industries=inp.get("industries"),
        locations=inp.get("locations"),
        status=inp.get("status"),
        max_companies=inp.get("max_companies", 50),
        include_founders=inp.get("include_founders", False),
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
