import os
import sys
import json
import logging
from typing import Optional
from apify_client import ApifyClient

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
from scrapers._apify import dataset_items, ApifyRunError  # noqa: E402

# Suppress verbose Apify actor log streaming
logging.getLogger("apify_client").setLevel(logging.WARNING)

# Swapped 2026-07-09 (twice): supreme_coder died actor-side → apimaestro batch
# ($5/1k) → harvestapi ($4/1k, 27k+ users, no cookies). Same output contract.
ACTOR_ID = "harvestapi/linkedin-profile-scraper"
_SCRAPER_MODE = "Profile details no email ($4 per 1k)"

_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def scrape_linkedin_profiles(profile_urls: list[str], max_profiles: int = 10) -> dict:
    """
    Scrapes LinkedIn profiles and returns structured data.

    Args:
        profile_urls: List of LinkedIn profile URLs
        max_profiles: Maximum number of profiles to process

    Returns:
        dict with keys: total, profiles, errors
    """
    api_token = os.environ.get("APIFY_API_TOKEN")
    if not api_token:
        raise EnvironmentError("APIFY_API_TOKEN environment variable is not set.")

    urls = profile_urls[:max_profiles]

    client = ApifyClient(api_token)

    actor_input = {"queries": urls, "profileScraperMode": _SCRAPER_MODE}

    print(f"Scraping {len(urls)} LinkedIn profile(s)...")
    run = client.actor(ACTOR_ID).call(run_input=actor_input)

    try:
        raw_items = dataset_items(client, run)
    except ApifyRunError as e:
        return {
            "total": 0,
            "profiles": [],
            "errors": [{"url": "all", "reason": str(e)}],
        }

    profiles = []
    errors = []

    for item in raw_items:
        if item.get("error") or not item.get("linkedinUrl"):
            errors.append({
                "url": item.get("linkedinUrl") or item.get("query", "unknown"),
                "reason": item.get("error", "No data returned")
            })
            continue

        # Current role — the actor exposes it directly.
        current = (item.get("currentPosition") or [{}])[0]
        current_company = None
        if current.get("companyName") or current.get("position"):
            current_company = {
                "name": current.get("companyName"),
                "title": current.get("position") or item.get("headline"),
            }

        work_history = [
            {
                "title": pos.get("position"),
                "company": pos.get("companyName"),
                "location": pos.get("location"),
                "start_date": _format_date(pos.get("startDate")),
                "end_date": _format_date(pos.get("endDate")),
                "description": pos.get("description")
            }
            for pos in (item.get("experience") or [])
        ]

        education = [
            {
                "school": edu.get("schoolName"),
                "degree": edu.get("degree"),
                "field_of_study": edu.get("fieldOfStudy"),
                "start_date": _format_date(edu.get("startDate")),
                "end_date": _format_date(edu.get("endDate"))
            }
            for edu in (item.get("education") or [])
        ]

        profiles.append({
            "url": item.get("linkedinUrl"),
            "full_name": f"{item.get('firstName', '')} {item.get('lastName', '')}".strip(),
            "headline": item.get("headline"),
            "about": item.get("about"),
            "location": (item.get("location") or {}).get("linkedinText"),
            "current_company": current_company,
            "work_history": work_history,
            "education": education
        })

    return {
        "total": len(profiles),
        "profiles": profiles,
        "errors": errors
    }


def _format_date(date_obj: Optional[dict]) -> Optional[str]:
    """Converts actor date object {year, month} to 'YYYY-MM' string.

    The actor returns month as a short name ("Feb") — older actors used ints;
    accept both."""
    if not date_obj:
        return None
    year = date_obj.get("year")
    month = date_obj.get("month")
    if isinstance(month, str):
        month = _MONTHS.get(month[:3].title())
    if year and month:
        return f"{year}-{int(month):02d}"
    if year:
        return str(year)
    return None


if __name__ == "__main__":
    # Accept input JSON file as argument, or fall back to example_input.json
    input_file = sys.argv[1] if len(sys.argv) > 1 else "example_input.json"

    with open(input_file) as f:
        input_data = json.load(f)

    result = scrape_linkedin_profiles(
        profile_urls=input_data["profile_urls"],
        max_profiles=input_data.get("max_profiles", 10)
    )

    print(json.dumps(result, indent=2, ensure_ascii=False))
