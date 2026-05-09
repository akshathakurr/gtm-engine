# LinkedIn Profile Scraper

Extracts professional details from LinkedIn public profiles using the Apify platform.

## What it returns
- Full name, headline, about/summary, location
- Current company and title
- Full work history (title, company, dates, description)
- Education history (school, degree, field of study, dates)

## Apify Actor
**ID:** `supreme_coder/linkedin-profile-scraper`
**Cost:** ~$0.003 per profile ($3 per 1,000)
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
- Only scrapes publicly visible profile data
- Private profiles or profiles with restricted visibility will return errors
- LinkedIn may throttle at very high volumes — stay under 10,000 profiles/day to be safe
