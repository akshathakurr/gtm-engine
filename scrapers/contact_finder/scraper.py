"""
Contact Finder — find email, phone, and professional details for a person or company.

Uses Apollo.io API (api.apollo.io).

Two modes:
  1. Person lookup  — POST /api/v1/people/match
     Input:  first_name + last_name + (domain OR organization_name) OR linkedin_url
     Output: email, phone, title, seniority, LinkedIn, location, employment history

  2. Company lookup — GET /api/v1/organizations/enrich
     Input:  domain
     Output: company name, industry, headcount, funding, technologies, location

Cost: Credits from your Apollo plan. Free plan: 1,200 credits/year (~100/month).
      people/match and organizations/enrich are accessible on paid plans.
      organizations/enrich works on free plan; people/match requires paid.

Auth: X-Api-Key header. Set APOLLO_API_KEY in /GTM Engine/.env
"""

import os
import sys
import json
import time
from typing import Optional, List

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "../../.env"))
APOLLO_API_KEY = os.getenv("APOLLO_API_KEY")
BASE_URL = "https://api.apollo.io/api/v1"


def _headers() -> dict:
    return {
        "X-Api-Key": APOLLO_API_KEY,
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
    }


# ---------------------------------------------------------------------------
# Person lookup
# ---------------------------------------------------------------------------

def find_contact(
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    domain: Optional[str] = None,
    organization_name: Optional[str] = None,
    linkedin_url: Optional[str] = None,
    reveal_personal_emails: bool = True,
    reveal_phone: bool = False,
) -> dict:
    """
    Find contact information for a specific person.

    Pass either (first_name + last_name + domain/organization_name) OR linkedin_url.

    Args:
        first_name:             Person's first name.
        last_name:              Person's last name.
        domain:                 Employer domain e.g. "acme.com" (no www/@).
        organization_name:      Employer name e.g. "Acme Corp". Use domain if possible — more accurate.
        linkedin_url:           LinkedIn profile URL. Overrides name/domain if provided.
        reveal_personal_emails: Include personal email addresses (default True).
        reveal_phone:           Include phone numbers (default False — costs extra credits).

    Returns:
        {
            "query": {...},
            "found": bool,
            "person": {
                "name", "email", "email_status", "phone",
                "title", "seniority", "departments",
                "linkedin_url", "twitter_url", "photo_url",
                "city", "state", "country",
                "employment_history": [...],
                "organization": {...}
            },
            "errors": [...]
        }
    """
    errors: List[str] = []

    if not APOLLO_API_KEY:
        return {"query": {}, "found": False, "person": None, "errors": ["APOLLO_API_KEY not set"]}

    payload: dict = {"reveal_personal_emails": reveal_personal_emails}
    if linkedin_url:
        payload["linkedin_url"] = linkedin_url
    else:
        if first_name:
            payload["first_name"] = first_name
        if last_name:
            payload["last_name"] = last_name
        if domain:
            payload["domain"] = domain
        if organization_name:
            payload["organization_name"] = organization_name
        if reveal_phone:
            payload["reveal_phone_number"] = True

    query = {k: v for k, v in payload.items() if k not in ("reveal_personal_emails", "reveal_phone_number")}

    try:
        resp = requests.post(
            f"{BASE_URL}/people/match",
            headers=_headers(),
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        if "error" in data:
            errors.append(data["error"])
            return {"query": query, "found": False, "person": None, "errors": errors}

        raw = data.get("person") or {}
        if not raw:
            return {"query": query, "found": False, "person": None, "errors": errors}

        person = _parse_person(raw)
        return {"query": query, "found": True, "person": person, "errors": errors}

    except Exception as e:
        errors.append(str(e))
        return {"query": query, "found": False, "person": None, "errors": errors}


def _parse_person(raw: dict) -> dict:
    org = raw.get("organization") or {}
    phones = raw.get("phone_numbers") or []
    primary_phone = phones[0].get("sanitized_number", "") if phones else ""

    employment = [
        {
            "title": e.get("title", ""),
            "organization": e.get("organization_name", ""),
            "start_date": e.get("start_date"),
            "end_date": e.get("end_date"),
            "current": bool(e.get("current")),
        }
        for e in (raw.get("employment_history") or [])
    ]

    return {
        "name": raw.get("name", ""),
        "first_name": raw.get("first_name", ""),
        "last_name": raw.get("last_name", ""),
        "email": raw.get("email", ""),
        "email_status": raw.get("email_status", ""),  # verified, unverified, bounced, etc.
        "phone": primary_phone,
        "title": raw.get("title", ""),
        "seniority": raw.get("seniority", ""),         # founder, c_suite, vp, director, manager, entry
        "departments": raw.get("departments") or [],
        "linkedin_url": raw.get("linkedin_url", ""),
        "twitter_url": raw.get("twitter_url", ""),
        "photo_url": raw.get("photo_url", ""),
        "city": raw.get("city", ""),
        "state": raw.get("state", ""),
        "country": raw.get("country", ""),
        "employment_history": employment,
        "organization": {
            "name": org.get("name", ""),
            "domain": org.get("primary_domain", ""),
            "industry": org.get("industry", ""),
            "headcount": org.get("estimated_num_employees"),
            "linkedin_url": org.get("linkedin_url", ""),
        },
    }


# ---------------------------------------------------------------------------
# Company lookup
# ---------------------------------------------------------------------------

def find_company(domain: str) -> dict:
    """
    Enrich a company by domain. Works on Apollo free plan.

    Args:
        domain: Company domain e.g. "acme.com" (no www/@).

    Returns:
        {
            "domain": str,
            "found": bool,
            "company": {
                "name", "domain", "industry", "headcount",
                "founded_year", "description",
                "linkedin_url", "twitter_url", "website_url",
                "city", "state", "country",
                "total_funding", "latest_funding_stage", "latest_funding_date",
                "technologies": [...],
                "keywords": [...]
            },
            "errors": [...]
        }
    """
    errors: List[str] = []

    if not APOLLO_API_KEY:
        return {"domain": domain, "found": False, "company": None, "errors": ["APOLLO_API_KEY not set"]}

    try:
        resp = requests.get(
            f"{BASE_URL}/organizations/enrich",
            headers=_headers(),
            params={"domain": domain},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        if "error" in data:
            errors.append(data["error"])
            return {"domain": domain, "found": False, "company": None, "errors": errors}

        raw = data.get("organization") or {}
        if not raw:
            return {"domain": domain, "found": False, "company": None, "errors": errors}

        company = _parse_company(raw)
        return {"domain": domain, "found": True, "company": company, "errors": errors}

    except Exception as e:
        errors.append(str(e))
        return {"domain": domain, "found": False, "company": None, "errors": errors}


def _parse_company(raw: dict) -> dict:
    techs = [t.get("name", "") for t in (raw.get("current_technologies") or [])]

    return {
        "name": raw.get("name", ""),
        "domain": raw.get("primary_domain", ""),
        "industry": raw.get("industry", ""),
        "headcount": raw.get("estimated_num_employees"),
        "founded_year": raw.get("founded_year"),
        "description": raw.get("short_description") or raw.get("seo_description", ""),
        "website_url": raw.get("website_url", ""),
        "linkedin_url": raw.get("linkedin_url", ""),
        "twitter_url": raw.get("twitter_url", ""),
        "city": raw.get("city", ""),
        "state": raw.get("state", ""),
        "country": raw.get("country", ""),
        "total_funding": raw.get("total_funding_printed", ""),
        "latest_funding_stage": raw.get("latest_funding_stage", ""),
        "latest_funding_date": raw.get("latest_funding_round_date", ""),
        "technologies": techs,
        "keywords": raw.get("keywords") or [],
    }


# ---------------------------------------------------------------------------
# Batch helpers
# ---------------------------------------------------------------------------

def find_contacts_batch(people: List[dict], delay: float = 1.0) -> List[dict]:
    """
    Run find_contact for a list of people. Each dict should have the same
    keys as find_contact() params.

    Adds a delay between calls to respect Apollo rate limits.
    """
    results = []
    for i, person in enumerate(people):
        if i > 0:
            time.sleep(delay)
        results.append(find_contact(**person))
    return results


def find_companies_batch(domains: List[str], delay: float = 0.5) -> List[dict]:
    """Run find_company for a list of domains."""
    results = []
    for i, domain in enumerate(domains):
        if i > 0:
            time.sleep(delay)
        results.append(find_company(domain))
    return results


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    input_file = sys.argv[1] if len(sys.argv) > 1 else "example_input.json"
    with open(input_file) as f:
        inp = json.load(f)

    mode = inp.get("mode", "person")

    if mode == "person":
        result = find_contact(
            first_name=inp.get("first_name"),
            last_name=inp.get("last_name"),
            domain=inp.get("domain"),
            organization_name=inp.get("organization_name"),
            linkedin_url=inp.get("linkedin_url"),
            reveal_personal_emails=inp.get("reveal_personal_emails", True),
            reveal_phone=inp.get("reveal_phone", False),
        )
    elif mode == "company":
        result = find_company(domain=inp["domain"])
    else:
        result = {"error": f"Unknown mode: {mode}. Use 'person' or 'company'."}

    print(json.dumps(result, indent=2, ensure_ascii=False))
