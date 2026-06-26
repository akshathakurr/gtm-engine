import os
import sys
import json
from contextlib import contextmanager
from urllib.parse import urlencode
from typing import Optional
from apify_client import ApifyClient

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
from scrapers._apify import dataset_items, ApifyRunError  # noqa: E402

ACTOR_ID = "curious_coder/linkedin-jobs-scraper"

# LinkedIn f_TPR values for time-posted filter
DAYS_TO_TPR = {1: "r86400", 7: "r604800", 30: "r2592000"}

DEFAULT_MAX_JOBS = 20
DEFAULT_DAYS_BACK = 30
DEFAULT_LOCATION = "United States"


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


def scrape_linkedin_jobs(
    keywords: Optional[str] = None,
    company_name: Optional[str] = None,
    location: str = DEFAULT_LOCATION,
    days_back: int = DEFAULT_DAYS_BACK,
    max_jobs: int = DEFAULT_MAX_JOBS,
    linkedin_url: Optional[str] = None,
) -> dict:
    """
    Fetches job postings from LinkedIn.

    Pass either `keywords` (+ optional company_name/location/days_back) to build
    the search automatically, or pass a direct `linkedin_url` to a LinkedIn job search page.

    Args:
        keywords:      Job title or role to search for (e.g. 'software engineer')
        company_name:  Company name to include in keywords filter (e.g. 'Acme Corp')
        location:      Location filter (default: 'United States')
        days_back:     Jobs posted in last N days — 1, 7, or 30 (default: 30)
        max_jobs:      Maximum jobs to return (default: 20)
        linkedin_url:  Direct LinkedIn job search URL. Overrides all other params if provided.

    Returns:
        dict with keys: query, total, jobs, errors
    """
    api_token = os.environ.get("APIFY_API_TOKEN")
    if not api_token:
        raise EnvironmentError("APIFY_API_TOKEN environment variable is not set.")

    if not linkedin_url and not keywords:
        raise ValueError("Either 'keywords' or 'linkedin_url' is required.")

    # Build the search URL if not provided directly
    if linkedin_url:
        search_url = linkedin_url
        query = {"url": linkedin_url}
    else:
        search_url = _build_search_url(keywords, company_name, location, days_back)
        query = {
            "keywords": keywords,
            "company_name": company_name,
            "location": location,
            "days_back": days_back,
            "url": search_url,
        }

    print(f"Searching: {search_url}")

    client = ApifyClient(api_token)

    actor_input = {
        "urls": [search_url],
        "maxResults": max_jobs,
    }

    with _suppress_apify_logs():
        run = client.actor(ACTOR_ID).call(run_input=actor_input)

    try:
        items = dataset_items(client, run)
    except ApifyRunError as e:
        return {"query": query, "total": 0, "jobs": [], "errors": [str(e)]}

    # Deduplicate by job ID, then cap at max_jobs
    seen_ids = set()
    unique_items = []
    for item in items:
        job_id = item.get("id")
        if job_id and job_id not in seen_ids:
            seen_ids.add(job_id)
            unique_items.append(item)
    items = unique_items[:max_jobs]

    jobs = [_parse_job(item) for item in items]

    return {
        "query": query,
        "total": len(jobs),
        "jobs": jobs,
        "errors": [],
    }


# ---------------------------------------------------------------------------
# URL builder
# ---------------------------------------------------------------------------

def _build_search_url(
    keywords: str,
    company_name: Optional[str],
    location: str,
    days_back: int,
) -> str:
    """Builds a LinkedIn job search URL from parameters."""
    # Combine keywords and company name into a single search string
    search_terms = keywords
    if company_name:
        search_terms = f"{keywords} {company_name}"

    tpr = DAYS_TO_TPR.get(days_back, "r2592000")

    params = {
        "keywords": search_terms,
        "location": location,
        "f_TPR": tpr,
        "position": 1,
        "pageNum": 0,
    }

    return "https://www.linkedin.com/jobs/search/?" + urlencode(params)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _parse_job(item: dict) -> dict:
    return {
        "id": item.get("id"),
        "title": item.get("title"),
        "company_name": item.get("companyName"),
        "company_linkedin_url": item.get("companyLinkedinUrl"),
        "location": item.get("location"),
        "posted_at": item.get("postedAt"),
        "job_url": item.get("link"),
        "description_html": item.get("descriptionHtml"),
        "benefits": item.get("benefits", []),
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    input_file = sys.argv[1] if len(sys.argv) > 1 else "example_input.json"

    with open(input_file) as f:
        input_data = json.load(f)

    result = scrape_linkedin_jobs(
        keywords=input_data.get("keywords"),
        company_name=input_data.get("company_name"),
        location=input_data.get("location", DEFAULT_LOCATION),
        days_back=input_data.get("days_back", DEFAULT_DAYS_BACK),
        max_jobs=input_data.get("max_jobs", DEFAULT_MAX_JOBS),
        linkedin_url=input_data.get("linkedin_url"),
    )

    print(json.dumps(result, indent=2, ensure_ascii=False))
