# LinkedIn Job Scraper

Extracts job postings from LinkedIn based on keywords, company, and recency.

## Actor

`curious_coder/linkedin-jobs-scraper` — $0.001/job, 44k users, 4.87 stars.

## Inputs

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `keywords` | string | Yes* | — | Job title or role (e.g. `software engineer`) |
| `company_name` | string | No | — | Company name to narrow search (appended to keywords) |
| `location` | string | No | `United States` | Location filter |
| `days_back` | integer | No | `30` | Recency filter — `1`, `7`, or `30` days |
| `max_jobs` | integer | No | `20` | Max jobs to return (1–200) |
| `linkedin_url` | string | Yes* | — | Pass a direct LinkedIn job search URL to bypass all other params |

*Either `keywords` or `linkedin_url` is required.

## Outputs

```json
{
  "query": { "keywords": "...", "company_name": "...", "url": "..." },
  "total": 20,
  "jobs": [
    {
      "id": "4381062426",
      "title": "Software Engineer I",
      "company_name": "Acme Corp",
      "company_linkedin_url": "https://www.linkedin.com/company/acme-corp",
      "location": "San Francisco, CA",
      "posted_at": "2026-03-28T15:22:33.000Z",
      "job_url": "https://www.linkedin.com/jobs/view/...",
      "description_html": "<strong>About the Role...</strong>",
      "benefits": ["Health insurance"]
    }
  ],
  "errors": []
}
```

## Usage

```bash
# Keyword + company search
APIFY_API_TOKEN=... python3 scraper.py

# Custom input file
APIFY_API_TOKEN=... python3 scraper.py my_input.json

# Direct LinkedIn URL
echo '{"linkedin_url": "https://www.linkedin.com/jobs/search/?keywords=engineer&f_C=1234"}' > input.json
APIFY_API_TOKEN=... python3 scraper.py input.json
```

## Notes

- Company filtering works by appending company name to the keyword string. This is fuzzy — may return jobs from similarly named companies. For precise company filtering, pass a direct `linkedin_url` with the `f_C=<company_id>` parameter.
- `days_back` only supports `1`, `7`, or `30` — these map to LinkedIn's native time filters.
- The actor fetches ~25 jobs minimum per run regardless of `max_jobs`. Cost is always at least ~$0.025/run.
- `description_html` contains the full job description as raw HTML.
