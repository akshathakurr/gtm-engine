"""
Email Outreach Workflow — Company Qualification + Enrichment + Prioritization + Email Outreach Prep

The canonical input is the **company** (name, optionally URL/LinkedIn). Person
info is optional and added later — only for companies that qualify (P0 by
default), since finding buyers, scraping posts, and writing copy is expensive.

Flow:
  1  — Enrich every company: URL, LinkedIn, one-liner description, employees,
       est revenue, founded year, total funding, HQ, 2-3 competitors
  2  — Score every company: ICP Segment + Priority (P0/P1/P2) + 1-line reasoning
  3  — Filter to outreach batch (P0 only by default; --include-p1 / --include-p2)
  4  — Find the buyer at each P0 company (name, position, LinkedIn) via web search
  5  — Classify buyer persona: Decision Maker / Champion / Non Decision Maker
  6  — Find email via Apollo Contact Finder
  7  — Small talk personalisation (Small Talk Scraper)
  8  — Scrape + filter LinkedIn posts by ICP relevance criteria
  9  — Generate personalisation hooks (Personalisation Hook Skill)
  10 — Write personalised email copy (Email Copy Writer Skill)

Usage:
  python -m workflows.email_outreach.workflow --sheet-id SHEET_ID
  python -m workflows.email_outreach.workflow --input-csv companies.csv --output-csv companies.out.csv
  python -m workflows.email_outreach.workflow --sheet-id SHEET_ID --include-p1
  python -m workflows.email_outreach.workflow --sheet-id SHEET_ID --limit 5   # quick test run
  python -m workflows.email_outreach.workflow --sheet-id SHEET_ID --enrich-fields employee_count,total_funding,hq
"""

import argparse
from typing import List, Dict

import anthropic

from workflows._common import (
    cell, load_icp,
    GoogleSheetsBackend, CsvBackend,
    detect_columns, cell_combined, get_or_create_col, parse_post_config,
    map_rate_limited,
)
from workflows.email_outreach.steps import (
    ENRICH_CONCURRENCY, APOLLO_MIN_INTERVAL, APOLLO_CONCURRENCY,
    POSTS_MIN_INTERVAL, POSTS_CONCURRENCY,
    _SMALL_TALK_AVAILABLE, _PERSONALISATION_AVAILABLE, _EMAIL_COPY_AVAILABLE,
    enrich_company, score_companies, find_buyer_at_company, find_linkedin_url,
    classify_personas, find_email, scrape_small_talk, scrape_and_filter_posts,
    generate_personalisation_hooks, write_email_copy,
)


# Snake_case keys are canonical; `label` is the default header name when the
# sheet has no semantic match. Add or remove an enrichment field in one place.
ENRICH_FIELDS: List[Dict[str, str]] = [
    {"key": "company_url",         "label": "Company URL",          "desc": "Company website URL (homepage)"},
    {"key": "company_linkedin",    "label": "Company LinkedIn URL", "desc": "Company LinkedIn page URL"},
    {"key": "company_description", "label": "Company Description",  "desc": "One-line description of what the company does"},
    {"key": "employee_count",      "label": "Employee Count",       "desc": "Headcount / number of employees"},
    {"key": "est_revenue",         "label": "Est Revenue",          "desc": "Estimated annual revenue"},
    {"key": "founded_year",        "label": "Founded Year",         "desc": "Year company was founded"},
    {"key": "total_funding",       "label": "Total Funding",        "desc": "Total funding raised"},
    {"key": "hq",                  "label": "HQ",                   "desc": "HQ city"},
    {"key": "competitors",         "label": "Competitors",          "desc": "2-3 immediate direct competitors, comma-separated"},
]

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _apply_enrichment(lead: Dict, enriched: Dict[str, str], fields: List[Dict[str, str]]) -> None:
    """Copy only the fields the run actually enriched onto the lead, so a
    restricted --enrich-fields run doesn't blank out untouched columns."""
    for f in fields:
        lead[f["key"]] = enriched.get(f["key"], "")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Email Outreach Workflow — Enrich, Prioritize, Find Buyer + Email, Prep Outreach"
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--sheet-id", default=None,
                     help="Google Sheet ID. Requires gws CLI installed and authed.")
    src.add_argument("--input-csv", default=None,
                     help="Path to a CSV file of companies (alternative to Sheets).")
    parser.add_argument("--sheet-name", default="Sheet1",
                        help="(Sheets only) Sheet tab name. Default: Sheet1")
    parser.add_argument("--output-csv", default=None,
                        help="(CSV only) Where to write output. Defaults to overwriting --input-csv.")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="Process only the first N rows (quick, cheap test runs)")
    parser.add_argument("--add-persona", action="append", default=[], metavar="TITLE",
                        help="One-time: also treat this title as a buyer (can repeat)")
    parser.add_argument("--remove-persona", action="append", default=[], metavar="TITLE",
                        help="One-time: exclude this title from buyer consideration (can repeat)")
    parser.add_argument("--enrich-fields", default=None,
                        help=f"Comma-separated enrichment field keys (default: {', '.join(f['key'] for f in ENRICH_FIELDS)})")
    parser.add_argument("--include-p1", action="store_true",
                        help="Also include P1 leads in outreach batch (default: P0 only)")
    parser.add_argument("--include-p2", action="store_true",
                        help="Also include P2 leads in outreach batch")
    parser.add_argument("--skip-enrich",     action="store_true")
    parser.add_argument("--skip-emails",     action="store_true",
                        help="Skip Apollo email lookup (Step 6)")
    parser.add_argument("--skip-small-talk", action="store_true")
    parser.add_argument("--skip-posts",      action="store_true")
    parser.add_argument("--skip-hooks",      action="store_true")
    parser.add_argument("--skip-copy",       action="store_true")
    args = parser.parse_args()

    client = anthropic.Anthropic()

    if args.skip_enrich:
        enrich_fields: List[Dict[str, str]] = []
    elif args.enrich_fields:
        wanted = {k.strip() for k in args.enrich_fields.split(",")}
        enrich_fields = [f for f in ENRICH_FIELDS if f["key"] in wanted]
    else:
        enrich_fields = list(ENRICH_FIELDS)

    print("\nLoading ICP context from context/...")
    icp_context = load_icp()
    post_config = parse_post_config(icp_context)
    print(f"  Post scraping config: max_posts={post_config['max_posts']}, days_back={post_config['days_back']}")

    if args.sheet_id:
        backend = GoogleSheetsBackend(args.sheet_id, args.sheet_name)
        print(f"Reading sheet {args.sheet_id} tab '{args.sheet_name}'...")
    else:
        backend = CsvBackend(args.input_csv, args.output_csv)
        out = args.output_csv or args.input_csv
        print(f"Reading CSV {args.input_csv} (writing to {out})...")
    rows = backend.read_all()
    if not rows or len(rows) < 2:
        print("Sheet/CSV is empty or has no data rows. Exiting.")
        return

    headers: List[str] = list(rows[0])
    data_rows: List[List[str]] = rows[1:]
    if args.limit is not None:
        data_rows = data_rows[:max(0, args.limit)]
        print(f"--limit {args.limit}: processing first {len(data_rows)} row(s).")
    print(f"Found {len(data_rows)} rows.")

    print("\nDetecting columns via Claude...")
    mapping = detect_columns(
        headers,
        data_rows[0] if data_rows else [],
        {
            # inputs (only company is required)
            "company":  "Current company name. REQUIRED.",
            "name":     "Full person name (combine first + last name columns if split). Optional.",
            "linkedin": "LinkedIn profile URL of the person. Optional.",
            "position": "Job title / role / position. Optional.",
            "email":    "Email address of the person. Optional.",
            # outputs (ok if missing — will be appended)
            "company_url":         "Company website URL (homepage)",
            "company_linkedin":    "Company LinkedIn page URL",
            "company_description": "One-line description of what the company does",
            "employee_count":      "Headcount / number of employees",
            "est_revenue":         "Estimated annual revenue",
            "founded_year":        "Year company was founded",
            "total_funding":       "Total funding raised",
            "hq":                  "HQ city",
            "competitors":         "2-3 immediate direct competitors of the company",
            "icp_segment":         "ICP segment / tier the company belongs to",
            "priority":            "Priority tier: P0 / P1 / P2",
            "reasoning":           "Reasoning explaining the priority",
            "buyer_persona":       "Buyer-persona classification: Decision Maker / Champion / Non DM",
            "post_links":          "LinkedIn post URLs of the lead",
            "small_talk":          "Small-talk / personalisation details",
            "hooks":               "Talking points / personalisation hooks",
            "copy":                "Final email copy / message",
        },
        client,
    )
    print(f"  Mapping: {mapping}")

    if not mapping.get("company"):
        print("ERROR: Could not detect a Company column. Company is the only required input.")
        print(f"Headers: {headers}")
        return

    leads: List[Dict] = []
    for row in data_rows:
        leads.append({
            "company":  cell_combined(row, mapping.get("company", [])),
            "name":     cell_combined(row, mapping.get("name", [])),
            "linkedin": cell_combined(row, mapping.get("linkedin", [])),
            "position": cell_combined(row, mapping.get("position", [])),
            "email":    cell_combined(row, mapping.get("email", [])),
        })

    # ------------------------------------------------------------------
    # Step 1: Enrich every company
    # ------------------------------------------------------------------
    if not enrich_fields:
        print("\n--- Step 1: Skipping enrichment (--skip-enrich) ---")
    else:
        print(f"\n--- Step 1: Enriching {len(leads)} company(ies) ---")
        print(f"  Fields: {', '.join(f['key'] for f in enrich_fields)}")

        col_idx_by_key: Dict[str, int] = {}
        for f in enrich_fields:
            idx = get_or_create_col(headers, mapping, f["key"], f["label"])
            col_idx_by_key[f["key"]] = idx
            backend.write_header(idx, headers[idx])

        company_cache: Dict[str, Dict[str, str]] = {}

        def _all_filled(row) -> bool:
            return all(
                col_idx_by_key[f["key"]] < len(row) and row[col_idx_by_key[f["key"]]].strip()
                for f in enrich_fields
            )

        # Enrich each UNIQUE company that still needs it, concurrently (Claude +
        # Exa). Deduping by name also avoids paying to enrich the same company
        # twice when multiple leads share it.
        to_enrich: List[str] = []
        seen_companies: set = set()
        for i, lead in enumerate(leads):
            company = lead["company"]
            if not company or _all_filled(data_rows[i]) or company in seen_companies:
                continue
            seen_companies.add(company)
            to_enrich.append(company)

        if to_enrich:
            print(f"  Enriching {len(to_enrich)} unique company(ies) in parallel...")
            results, errors = map_rate_limited(
                lambda c: enrich_company(c, enrich_fields, client),
                to_enrich, max_workers=ENRICH_CONCURRENCY,
            )
            for c, r, e in zip(to_enrich, results, errors):
                if e:
                    print(f"    ! {c} enrichment failed: {e}")
                company_cache[c] = r or {}

        # Apply + write sequentially so the backend is never touched concurrently.
        for i, lead in enumerate(leads):
            company = lead["company"]
            if not company:
                continue
            row     = data_rows[i]
            row_num = i + 2
            if _all_filled(row):
                enriched = {f["key"]: cell(row, col_idx_by_key[f["key"]]) for f in enrich_fields}
            else:
                enriched = company_cache.get(company, {})
                for f in enrich_fields:
                    backend.write_cell(row_num, col_idx_by_key[f["key"]], enriched.get(f["key"], ""))
            _apply_enrichment(leads[i], enriched, enrich_fields)

    # ------------------------------------------------------------------
    # Step 2: Score (ICP Segment + Priority + Reasoning)
    # ------------------------------------------------------------------
    print("\n--- Step 2: Scoring companies (ICP Segment + Priority) ---")
    scores = score_companies(leads, icp_context, client)

    icp_col_idx       = get_or_create_col(headers, mapping, "icp_segment", "ICP Segment")
    priority_col_idx  = get_or_create_col(headers, mapping, "priority",    "Priority")
    reasoning_col_idx = get_or_create_col(headers, mapping, "reasoning",   "Reasoning")
    backend.write_header(icp_col_idx,       headers[icp_col_idx])
    backend.write_header(priority_col_idx,  headers[priority_col_idx])
    backend.write_header(reasoning_col_idx, headers[reasoning_col_idx])
    backend.write_column(icp_col_idx,       [s.get("icp_segment", "") for s in scores])
    backend.write_column(priority_col_idx,  [s["priority"] for s in scores])
    backend.write_column(reasoning_col_idx, [s["reasoning"] for s in scores])

    p0 = sum(1 for s in scores if s["priority"] == "P0")
    p1 = sum(1 for s in scores if s["priority"] == "P1")
    p2 = sum(1 for s in scores if s["priority"] == "P2")
    print(f"  P0: {p0} | P1: {p1} | P2: {p2}")

    # ------------------------------------------------------------------
    # Step 3: Filter outreach batch (P0 only by default)
    # ------------------------------------------------------------------
    print("\n--- Step 3: Selecting outreach batch ---")
    tiers = {"P0"}
    if args.include_p1:
        tiers.add("P1")
    if args.include_p2:
        tiers.add("P2")

    outreach_indices = [i for i, s in enumerate(scores) if s["priority"] in tiers]
    print(f"  Tiers in batch: {sorted(tiers)} → {len(outreach_indices)} leads")
    if not outreach_indices:
        print("  No outreach leads selected. Exiting.")
        return

    # Step-spanning caches
    post_data_by_lead:  Dict[int, List[Dict]] = {}
    small_talk_by_lead: Dict[int, str]        = {}
    hooks_by_lead:      Dict[int, str]        = {}
    classifications:    Dict[int, str]        = {}  # filled in Step 5
    email_by_lead:      Dict[int, str]        = {}  # filled in Step 6

    # ------------------------------------------------------------------
    # Step 4: Find buyer at each P0 company
    # ------------------------------------------------------------------
    print(f"\n--- Step 4: Finding buyers for {len(outreach_indices)} leads ---")

    name_col_idx     = get_or_create_col(headers, mapping, "name",     "Name")
    position_col_idx = get_or_create_col(headers, mapping, "position", "Position")
    linkedin_col_idx = get_or_create_col(headers, mapping, "linkedin", "LinkedIn Profile")
    backend.write_header(name_col_idx,     headers[name_col_idx])
    backend.write_header(position_col_idx, headers[position_col_idx])
    backend.write_header(linkedin_col_idx, headers[linkedin_col_idx])

    # Each lead's buyer lookup is independent Claude + Exa work — compute in
    # parallel, then write sequentially.
    def _buyer_task(i: int):
        lead = leads[i]
        if (lead.get("name") or "").strip():
            if not (lead.get("linkedin") or "").strip():
                return ("linkedin", find_linkedin_url(lead["name"], lead["company"], client))
            return ("skip", None)
        return ("buyer", find_buyer_at_company(lead["company"], icp_context, client))

    buyer_results, buyer_errors = map_rate_limited(
        _buyer_task, outreach_indices, max_workers=ENRICH_CONCURRENCY,
    )
    for i, r, e in zip(outreach_indices, buyer_results, buyer_errors):
        lead    = leads[i]
        row_num = i + 2
        if e:
            print(f"  {lead['company']} — buyer lookup failed: {e}")
            continue
        kind, val = r
        if kind == "skip":
            print(f"  {lead['name']} ({lead['company']}) — already filled, skipping")
        elif kind == "linkedin":
            if val:
                lead["linkedin"] = val
                backend.write_cell(row_num, linkedin_col_idx, val)
                print(f"  {lead['name']} ({lead['company']}) → {val}")
        else:  # buyer
            buyer = val or {}
            if buyer.get("name"):
                lead["name"]     = buyer["name"]
                lead["position"] = buyer.get("position", "")
                lead["linkedin"] = buyer.get("linkedin", "")
                backend.write_cell(row_num, name_col_idx,     lead["name"])
                backend.write_cell(row_num, position_col_idx, lead["position"])
                if lead["linkedin"]:
                    backend.write_cell(row_num, linkedin_col_idx, lead["linkedin"])
                print(f"  {lead['company']} → {lead['name']} ({lead['position']})")
            else:
                print(f"  {lead['company']} → could not find a buyer")

    # ------------------------------------------------------------------
    # Step 5: Classify buyer persona — only for P0 with a buyer
    # ------------------------------------------------------------------
    print("\n--- Step 5: Classifying buyer personas ---")
    if args.add_persona:
        print(f"  + Adding for this run: {args.add_persona}")
    if args.remove_persona:
        print(f"  - Removing for this run: {args.remove_persona}")

    persona_inputs = [(i, leads[i]) for i in outreach_indices if (leads[i].get("name") or "").strip()]
    if persona_inputs:
        sub_leads = [l for _, l in persona_inputs]
        sub_cls   = classify_personas(
            sub_leads, icp_context, args.add_persona, args.remove_persona, client,
        )
        buyer_col_idx = get_or_create_col(headers, mapping, "buyer_persona", "Buyer Persona Match")
        backend.write_header(buyer_col_idx, headers[buyer_col_idx])
        for (i, _), c in zip(persona_inputs, sub_cls):
            classifications[i] = c
            backend.write_cell(i + 2, buyer_col_idx, c)

        dm_count    = sum(1 for c in classifications.values() if c == "Decision Maker")
        champ_count = sum(1 for c in classifications.values() if c == "Champion")
        ndm_count   = sum(1 for c in classifications.values() if c == "Non Decision Maker")
        print(f"  DMs: {dm_count} | Champions: {champ_count} | Non DMs: {ndm_count}")
    else:
        print("  No P0 leads with a buyer to classify — skipping.")
        dm_count = champ_count = ndm_count = 0

    # ------------------------------------------------------------------
    # Step 6: Find emails via Apollo
    # ------------------------------------------------------------------
    if args.skip_emails:
        print("\n--- Step 6: Skipping email lookup (--skip-emails) ---")
    else:
        print(f"\n--- Step 6: Finding emails for {len(outreach_indices)} leads ---")

        email_col_idx = get_or_create_col(headers, mapping, "email", "Email")
        backend.write_header(email_col_idx, headers[email_col_idx])

        # Decide who still needs a lookup; reuse anything already present.
        need_email: List[int] = []
        for i in outreach_indices:
            lead = leads[i]
            row  = data_rows[i]
            if not (lead.get("name") or "").strip():
                print(f"  {lead['company']} — no buyer, skipping email lookup")
                continue
            existing = cell(row, email_col_idx) if email_col_idx < len(row) else ""
            if existing or (lead.get("email") or "").strip():
                email_by_lead[i] = existing or lead["email"]
                continue
            need_email.append(i)

        # Apollo is rate-limited — space lookups APOLLO_MIN_INTERVAL apart, but
        # overlap their latency across a small pool.
        results, errors = map_rate_limited(
            lambda i: find_email(leads[i]), need_email,
            min_interval=APOLLO_MIN_INTERVAL, max_workers=APOLLO_CONCURRENCY,
        )
        for i, r, e in zip(need_email, results, errors):
            email = "" if e else (r or "")
            if e:
                print(f"  {leads[i]['name']} — email lookup failed: {e}")
            email_by_lead[i] = email
            leads[i]["email"] = email
            backend.write_cell(i + 2, email_col_idx, email)
            print(f"  {leads[i]['name']} ({leads[i]['company']}) → {email or '(not found)'}")

    # ------------------------------------------------------------------
    # Step 7: Small talk
    # ------------------------------------------------------------------
    if args.skip_small_talk:
        print("\n--- Step 7: Skipping small talk (--skip-small-talk) ---")
    elif not _SMALL_TALK_AVAILABLE:
        print("\n--- Step 7: Small Talk Scraper failed to import — skipping ---")
    else:
        print(f"\n--- Step 7: Gathering small talk for {len(outreach_indices)} leads ---")

        small_talk_col_idx = get_or_create_col(headers, mapping, "small_talk", "Small Talk")
        backend.write_header(small_talk_col_idx, headers[small_talk_col_idx])

        st_indices = [i for i in outreach_indices if (leads[i].get("name") or "").strip()]
        results, errors = map_rate_limited(
            lambda i: scrape_small_talk(
                profile_url=leads[i].get("linkedin", ""), name=leads[i]["name"], company=leads[i]["company"],
            ),
            st_indices, max_workers=ENRICH_CONCURRENCY,
        )
        for i, r, e in zip(st_indices, results, errors):
            detail = "" if e else (r or "")
            if e:
                print(f"  {leads[i]['name']} — small talk failed: {e}")
            small_talk_by_lead[i] = detail
            backend.write_cell(i + 2, small_talk_col_idx, detail)
            print(f"  {leads[i]['name']} ({leads[i]['company']}) → {(detail[:80] + '...') if len(detail) > 80 else (detail or '(none)')}")

    # ------------------------------------------------------------------
    # Step 8: Posts (LinkedIn) — needs lead.linkedin
    # ------------------------------------------------------------------
    if args.skip_posts:
        print("\n--- Step 8: Skipping post scraping (--skip-posts) ---")
    else:
        has_linkedin = [i for i in outreach_indices if (leads[i].get("linkedin") or "").strip()]
        print(f"\n--- Step 8: Scraping LinkedIn posts for {len(has_linkedin)} leads ---")
        print(f"  Config: {post_config['max_posts']} posts / {post_config['days_back']} days")

        post_links_col_idx = get_or_create_col(headers, mapping, "post_links", "LinkedIn Post Links")
        backend.write_header(post_links_col_idx, headers[post_links_col_idx])

        # profile-posts actor throttles on bursts — space starts POSTS_MIN_INTERVAL
        # apart, overlap run-times across a small pool.
        results, errors = map_rate_limited(
            lambda i: scrape_and_filter_posts(
                profile_url=leads[i]["linkedin"], icp_context=icp_context,
                max_posts=post_config["max_posts"], days_back=post_config["days_back"], client=client,
            ),
            has_linkedin, min_interval=POSTS_MIN_INTERVAL, max_workers=POSTS_CONCURRENCY,
        )
        for i, r, e in zip(has_linkedin, results, errors):
            if e:
                print(f"  {leads[i]['name']} — post scraping failed: {e}")
                post_data_by_lead[i] = []
                continue
            post_data_by_lead[i] = r["posts_data"]
            cell_value = "\n".join(r["urls"]) if r["urls"] else ""
            backend.write_cell(i + 2, post_links_col_idx, cell_value)
            print(f"  {leads[i]['name']} ({leads[i]['company']}) → {len(r['urls'])} post(s) matched")

    # ------------------------------------------------------------------
    # Step 9: Personalisation hooks
    # ------------------------------------------------------------------
    if args.skip_hooks:
        print("\n--- Step 9: Skipping personalisation hooks (--skip-hooks) ---")
    elif not _PERSONALISATION_AVAILABLE:
        print("\n--- Step 9: Personalisation Hook Skill failed to import — skipping ---")
    else:
        print(f"\n--- Step 9: Generating personalisation hooks for {len(outreach_indices)} leads ---")

        hooks_col_idx = get_or_create_col(headers, mapping, "hooks", "Personalisation Hook")
        backend.write_header(hooks_col_idx, headers[hooks_col_idx])

        hook_indices = [i for i in outreach_indices if (leads[i].get("name") or "").strip()]

        def _hook_task(i: int) -> str:
            lead = leads[i]
            return generate_personalisation_hooks(
                name=lead["name"], company=lead["company"], position=lead["position"],
                matching_posts=post_data_by_lead.get(i, []),
                small_talk=small_talk_by_lead.get(i, ""),
                icp_context=icp_context,
                competitors=lead.get("competitors", ""),
                company_description=lead.get("company_description", ""),
                employee_count=lead.get("employee_count", ""),
                est_revenue=lead.get("est_revenue", ""),
                total_funding=lead.get("total_funding", ""),
                hq=lead.get("hq", ""),
            )

        results, errors = map_rate_limited(_hook_task, hook_indices, max_workers=ENRICH_CONCURRENCY)
        for i, r, e in zip(hook_indices, results, errors):
            hooks = "" if e else (r or "")
            if e:
                print(f"  {leads[i]['name']} — hook generation failed: {e}")
            hooks_by_lead[i] = hooks
            backend.write_cell(i + 2, hooks_col_idx, hooks)
            print(f"  {leads[i]['name']} ({leads[i]['company']}) → {(hooks[:100] + '...') if len(hooks) > 100 else (hooks or '(none)')}")

    # ------------------------------------------------------------------
    # Step 10: Email copy
    # ------------------------------------------------------------------
    if args.skip_copy:
        print("\n--- Step 10: Skipping email copy (--skip-copy) ---")
    elif not _EMAIL_COPY_AVAILABLE:
        print("\n--- Step 10: Email Copy Writer Skill failed to import — skipping ---")
    else:
        print(f"\n--- Step 10: Writing email copy for {len(outreach_indices)} leads ---")

        copy_col_idx = get_or_create_col(headers, mapping, "copy", "Email Copy")
        backend.write_header(copy_col_idx, headers[copy_col_idx])

        copy_indices = [i for i in outreach_indices if (leads[i].get("name") or "").strip()]

        def _copy_task(i: int) -> str:
            lead = leads[i]
            return write_email_copy(
                name=lead["name"], company=lead["company"], position=lead["position"],
                email=email_by_lead.get(i, lead.get("email", "")),
                buyer_persona=classifications.get(i, ""),
                priority=scores[i]["priority"],
                matching_posts=post_data_by_lead.get(i, []),
                small_talk=small_talk_by_lead.get(i, ""),
                personalisation_hook=hooks_by_lead.get(i, ""),
                icp_context=icp_context,
                employee_count=lead.get("employee_count", ""),
                est_revenue=lead.get("est_revenue", ""),
                total_funding=lead.get("total_funding", ""),
                hq=lead.get("hq", ""),
                competitors=lead.get("competitors", ""),
            )

        results, errors = map_rate_limited(_copy_task, copy_indices, max_workers=ENRICH_CONCURRENCY)
        for i, r, e in zip(copy_indices, results, errors):
            copy = "" if e else (r or "")
            if e:
                print(f"  {leads[i]['name']} — copy generation failed: {e}")
            backend.write_cell(i + 2, copy_col_idx, copy)
            print(f"  {leads[i]['name']} ({leads[i]['company']}) → {(copy[:100] + '...') if len(copy) > 100 else (copy or '(none)')}")

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    print("\n======= Done =======")
    print(f"  Companies processed:  {len(leads)}")
    print(f"  Priority scores:      {p0} P0 | {p1} P1 | {p2} P2")
    print(f"  Outreach batch:       {len(outreach_indices)} leads")
    print(f"  Buyer personas (P0):  {dm_count} DMs | {champ_count} Champions | {ndm_count} Non DMs")


if __name__ == "__main__":
    main()
