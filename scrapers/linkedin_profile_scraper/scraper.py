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

ACTOR_ID = "supreme_coder/linkedin-profile-scraper"


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

    actor_input = {
        "urls": [{"url": u} for u in urls],
    }

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
        if item.get("error") or not item.get("inputUrl"):
            errors.append({
                "url": item.get("inputUrl", "unknown"),
                "reason": item.get("error", "No data returned")
            })
            continue

        # Current role
        positions_raw = item.get("positions") or []
        current_company = None
        if positions_raw:
            first = positions_raw[0]
            current_company = {
                "name": first.get("company", {}).get("name") or item.get("companyName"),
                "title": first.get("title") or item.get("jobTitle")
            }

        work_history = [
            {
                "title": pos.get("title"),
                "company": pos.get("company", {}).get("name"),
                "location": pos.get("locationName"),
                "start_date": _format_date(pos.get("timePeriod", {}).get("startDate")),
                "end_date": _format_date(pos.get("timePeriod", {}).get("endDate")),
                "description": pos.get("description")
            }
            for pos in positions_raw
        ]

        education_raw = item.get("educations") or []
        education = [
            {
                "school": edu.get("schoolName"),
                "degree": edu.get("degreeName"),
                "field_of_study": edu.get("fieldOfStudy"),
                "start_date": _format_date(edu.get("timePeriod", {}).get("startDate")),
                "end_date": _format_date(edu.get("timePeriod", {}).get("endDate"))
            }
            for edu in education_raw
        ]

        profiles.append({
            "url": item.get("inputUrl"),
            "full_name": f"{item.get('firstName', '')} {item.get('lastName', '')}".strip(),
            "headline": item.get("headline"),
            "about": item.get("summary"),
            "location": item.get("geoLocationName"),
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
    """Converts Apify date object {year, month} to 'YYYY-MM' string."""
    if not date_obj:
        return None
    year = date_obj.get("year")
    month = date_obj.get("month")
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
