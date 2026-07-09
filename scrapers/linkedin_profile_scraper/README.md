# LinkedIn Profile Scraper

Extracts professional details from LinkedIn public profiles using the Apify platform.

## What it returns
- Full name, headline, about/summary, location
- Current company and title
- Full work history (title, company, dates, description)
- Education history (school, degree, field of study, dates)

## Apify Actor
**ID:** `harvestapi/linkedin-profile-scraper`
**Cost:** $0.004 per profile ($4 per 1,000)
**Login required:** No — works on public profiles without LinkedIn cookies

## Setup

Install the Apify client:
```bash
pip install apify-client
```

Set your API token:
```bash
export APIFY_API_TOKEN=your_token_here
```

## Usage

**From another workflow (import):**
```python
from Scrapers.LinkedIn_Profile_Scraper.scraper import scrape_linkedin_profiles

result = scrape_linkedin_profiles(
    profile_urls=["https://www.linkedin.com/in/satyanadella/"],
    max_profiles=10
)
```

**From the command line:**
```bash
# Uses example_input.json by default
python scraper.py

# Or pass a custom input file
python scraper.py my_input.json
```

## Input
See `input_schema.json` for full schema. Required field: `profile_urls` (list of LinkedIn URLs).

## Output
See `output_schema.json` for full schema and `example_output.json` for a sample.

## Notes
- **Actor output change (observed 2026-07-06):** the actor no longer returns
  `firstName`/`lastName`/`headline`/`geoLocationName`, so `full_name`,
  `headline`, and `location` in our output are now empty/None. Job title,
  company, work history, education, and summary still come through
  (see the refreshed `raw_sample.json`). No workflow currently consumes this
  scraper; if names become needed, swap actors.
- **Account requirement:** on some Apify accounts this actor fails with
  "Proxy authentication required" (residential-proxy restriction) — verified
  account-dependent 2026-07-06, not an input problem.
- Only scrapes publicly visible profile data
- Private profiles or profiles with restricted visibility will return errors
- LinkedIn may throttle at very high volumes — stay under 10,000 profiles/day to be safe
