# gtm engine

your GTM team is a folder of python scripts and a markdown file describing your business. open it in claude code, answer some questions, ship campaigns.

**this is the sales / marketing / content stack you actually own.**

no SaaS subscriptions, no data lock-in, no per-seat pricing. you bring an Anthropic key and an Apify token. claude code drives everything.

## what it does

7 workflows that cover most of GTM:

| workflow | what it does | output |
|---|---|---|
| linkedin outreach | find leads, scrape their posts, write personal DMs | google sheet of leads + drafted messages |
| email outreach | enrich a list of companies, write personalized cold emails | csv ready for instantly / smartlead |
| competitor analysis | research 12 dimensions of every competitor | filled-in google sheet (firmographics, founders, GTM, scoring) |
| content idea finder | scan twitter + HN daily, cluster into post ideas | 5 daily ideas, classified by genre + platform |
| linkedin comment helper | surface LinkedIn posts worth commenting on | ranked list with suggested takes |
| blog builder | research a topic deeply, draft a blog post | full article + sources |
| signal-based outreach | find leads by buying signal (hiring, funding, launches) | enriched lead list |

## how to use it

1. install [claude code](https://claude.com/claude-code)
2. clone this repo
3. open the folder in claude code
4. say `help me get started`

claude code reads the questionnaire, asks the questions to you, fills in your business context, then asks which workflow to run. that's the whole UX. no python required.

if you'd rather run things directly, every workflow has its own README with the exact `python -m ...` commands.

## what you'll need

- **anthropic api key** — claude does the thinking. ~$1–5 per workflow run depending on which one. [console.anthropic.com](https://console.anthropic.com)
- **apify token** — for LinkedIn / Twitter / review scraping. pay-per-run, usually $0.50–$3 per workflow. [apify.com](https://apify.com)
- **exa key** *(optional)* — for web research. only blog builder + competitor analysis need it. [exa.ai](https://exa.ai)
- **google account** *(optional)* — most workflows can write to a google sheet. csv mode works if you'd rather not.

cost-per-run is in each workflow's README.

## why this exists

most GTM tools are subscription products that own your data, your prospect list, and your messaging. you pay $200/seat/month for software that does what a smart prompt and a scraper could do — and you can't take any of it with you when you leave.

this is the inverse: workflows you can read, modify, fork, and run on your own infrastructure with your own keys. when claude gets better, this gets better. when you change products, you edit a markdown file.

claude code makes the whole thing usable without writing python. your "team" is a folder.

## what's inside

```
context/
  context.md.example       the questionnaire claude code walks you through
  context.md               your business — product, ICP, competitors, tone of voice (gitignored)

workflows/
  linkedin_outreach/
  email_outreach/
  competitor_analysis/
  content_idea_finder/
  linkedin_comment_helper/
  blog_builder/
  signal_based_lead_outreach/

scrapers/                  single-source data fetchers (LinkedIn, Twitter, G2…)
skills/                    reusable claude prompt modules
```

every workflow folder has its own README — open it for the full details.

## status

early. opinionated. some workflows are battle-tested, others are stubs being filled in. expect rough edges; PRs welcome.

most-tested today: linkedin outreach · email outreach · competitor analysis · content idea finder.

## license

MIT
