# Contact Finder

Find email, phone, and professional details for a person or company using the Apollo.io API.

Two modes in one scraper:
- **Person mode** — name + domain → email, title, seniority, employment history
- **Company mode** — domain → funding, headcount, tech stack, industry

---

## API

**Apollo.io** — `api.apollo.io`

Set `APOLLO_API_KEY` in `/GTM Engine/.env`.

**Free plan:** 1,200 credits/year (~100/month). Company enrichment works on free. Person lookup requires a paid plan.

---

## Inputs

| Field | Type | Default | Description |
|---|---|---|---|
| `mode` | string | `"person"` | `"person"` or `"company"` |
| `first_name` | string | — | Person's first name |
| `last_name` | string | — | Person's last name |
| `domain` | string | — | Employer or company domain e.g. `"acme.com"` (no www/@) |
| `organization_name` | string | — | Employer name. Use `domain` if possible — more accurate. |
| `linkedin_url` | string | — | LinkedIn URL. Overrides name+domain if provided. |
| `reveal_personal_emails` | bool | `true` | Include personal emails |
| `reveal_phone` | bool | `false` | Include phone numbers (costs extra credits) |

For `mode="company"`, only `domain` is needed.

## Outputs

**Person mode:**
- `name`, `email`, `email_status` (verified/unverified/bounced), `phone`
- `title`, `seniority` (founder/c_suite/vp/director/manager/entry), `departments`
- `linkedin_url`, `twitter_url`, `city`, `state`, `country`
- `employment_history[]` — title, org, start/end dates, current flag
- `organization{}` — name, domain, industry, headcount

**Company mode:**
- `name`, `domain`, `industry`, `headcount`, `founded_year`, `description`
- `website_url`, `linkedin_url`, `twitter_url`
- `city`, `state`, `country`
- `total_funding`, `latest_funding_stage`, `latest_funding_date`
- `technologies[]`, `keywords[]`

---

## Usage

```bash
# Person lookup
python3 scraper.py example_input.json

# Company lookup
echo '{"mode": "company", "domain": "notion.so"}' > /tmp/co.json
python3 scraper.py /tmp/co.json
```

## Batch

```python
from scraper import find_contacts_batch, find_companies_batch

# Find emails for multiple people
contacts = find_contacts_batch([
    {"first_name": "Jane", "last_name": "Smith", "domain": "acme.com"},
    {"first_name": "John", "last_name": "Doe", "linkedin_url": "https://linkedin.com/in/johndoe"},
])

# Enrich multiple companies
companies = find_companies_batch(["notion.so", "linear.app", "vercel.com"])
```

## Notes

- `domain` is preferred over `organization_name` — Apollo matches on domain more reliably.
- `email_status: "verified"` means SMTP-verified. Use this field to filter before outreach.
- `reveal_phone` costs extra credits — leave False unless you specifically need phone numbers.
- Person lookup (`people/match`) requires a paid Apollo plan. Company enrichment (`organizations/enrich`) works on free.
- Apollo rate limits vary by plan. The batch helpers add a 1s delay between person calls and 0.5s between company calls.
