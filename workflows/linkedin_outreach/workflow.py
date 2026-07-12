"""
LinkedIn Outreach Workflow — Buyer Qualification + Enrichment + Prioritization + Outreach Prep

Steps:
  1  — Classify each lead: Decision Maker / Champion / Non Decision Maker
  2  — Enrich Decision Maker companies with firmographic data via Web Search
  3  — Score all leads P0 / P1 / P2 with 1-2 line reasoning
  4  — Select outreach batch: P0 only by default (--include-p1 / --include-p2 widen it)
  5  — (opt-in, --with-competitors) Find 3-4 direct competitors for each filtered lead's company (Web Search)
  6  — Scrape LinkedIn posts for each filtered lead; filter by ICP criteria; write post URLs
  7  — Gather small talk details for each filtered lead (Small Talk Scraper)
  8  — Generate personalisation talking points (Personalisation Hook Skill)
  9  — Write personalised LinkedIn copy (LinkedIn Copy Writer Skill)

Buyer criteria, ICP scoring, and post filtering criteria come from the context/*.md files.
Post scraping config (max_posts, days_back) is also read from those files — with scraper defaults
as fallback if the fields are not filled in.

Usage:
  python -m workflows.linkedin_outreach.workflow --sheet-id SHEET_ID
  python -m workflows.linkedin_outreach.workflow --sheet-id SHEET_ID --sheet-name "Leads"
  python -m workflows.linkedin_outreach.workflow --sheet-id SHEET_ID --limit 5   # quick test run
  python -m workflows.linkedin_outreach.workflow --sheet-id SHEET_ID --add-persona "VP Sales"
  python -m workflows.linkedin_outreach.workflow --sheet-id SHEET_ID --remove-persona "Founder"
  python -m workflows.linkedin_outreach.workflow --sheet-id SHEET_ID --enrich-columns "Employee Count,HQ"
  python -m workflows.linkedin_outreach.workflow --sheet-id SHEET_ID --skip-enrich
  python -m workflows.linkedin_outreach.workflow --sheet-id SHEET_ID --skip-posts
  python -m workflows.linkedin_outreach.workflow --sheet-id SHEET_ID --skip-small-talk
"""

import argparse
from typing import List, Dict, Optional

import anthropic

from workflows._common import (
    col_letter, cell, load_icp,
    GoogleSheetsBackend, CsvBackend,
    detect_columns, cell_combined, get_or_create_col, parse_post_config,
    map_rate_limited, checkpoint_path, checkpoint_load, checkpoint_append,
    MilestoneFlusher,
)
from workflows.linkedin_outreach.steps import (
    ENRICH_CONCURRENCY, POSTS_MIN_INTERVAL, POSTS_CONCURRENCY,
    _SMALL_TALK_AVAILABLE, _PERSONALISATION_AVAILABLE, _COPY_WRITER_AVAILABLE,
    find_missing_lead_data, classify_personas, enrich_company, score_leads,
    find_competitors, scrape_and_filter_posts, generate_personalisation_hooks,
    write_linkedin_copy, scrape_small_talk,
)

# Enrichment fields. Snake_case keys are used throughout the code; the `label`
# is only used as the default header name when the sheet has no semantic match.
# Add or remove fields here in one place.
ENRICH_FIELDS: List[Dict[str, str]] = [
    {"key": "company_url",         "label": "Company URL",          "desc": "Company website URL (homepage)"},
    {"key": "company_linkedin",    "label": "Company LinkedIn URL", "desc": "Company LinkedIn page URL"},
    {"key": "company_description", "label": "Company Description",  "desc": "One-line description of what the company does"},
    {"key": "employee_count",      "label": "Employee Count",       "desc": "Headcount / number of employees"},
    {"key": "est_revenue",         "label": "Est Revenue",          "desc": "Estimated annual revenue"},
    {"key": "founded_year",        "label": "Founded Year",         "desc": "Year company was founded"},
    {"key": "total_funding",       "label": "Total Funding",        "desc": "Total funding raised"},
    {"key": "hq",                  "label": "HQ",                   "desc": "HQ city"},
]
# Output schema for score_leads — same pattern.
SCORE_FIELDS: List[Dict[str, str]] = [
    {"key": "priority",    "label": "Priority",    "desc": "Priority tier P0 / P1 / P2"},
    {"key": "icp_segment", "label": "ICP Segment", "desc": "ICP segment / tier name from the ICP context"},
    {"key": "reasoning",   "label": "Reasoning",   "desc": "1-2 sentences explaining the priority"},
]

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _apply_enrichment(lead: Dict, enriched: Dict[str, str]) -> None:
    """Copy all enrichment values into the lead dict using snake_case keys."""
    for f in ENRICH_FIELDS:
        lead[f["key"]] = enriched.get(f["key"], "")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="LinkedIn Outreach Workflow — Qualify, Enrich, Prioritize, Prep Outreach"
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--sheet-id", default=None,
                     help="Google Sheet ID (from the URL). Requires the gws CLI to be installed and authed.")
    src.add_argument("--input-csv", default=None,
                     help="Path to a CSV file of leads. Use this if you don't want to use Google Sheets.")
    parser.add_argument("--sheet-name", default="Sheet1",
                        help="(Sheets only) Sheet tab name. Default: Sheet1")
    parser.add_argument("--output-csv", default=None,
                        help="(CSV only) Where to write enriched output. Defaults to overwriting --input-csv.")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="Process only the first N leads (quick, cheap test runs)")
    parser.add_argument("--add-persona", action="append", default=[], metavar="TITLE",
                        help="One-time: also treat this title as a buyer (can repeat)")
    parser.add_argument("--remove-persona", action="append", default=[], metavar="TITLE",
                        help="One-time: exclude this title from buyer consideration (can repeat)")
    parser.add_argument("--enrich-fields", default=None,
                        help=f"Comma-separated enrichment field keys (default: {', '.join(f['key'] for f in ENRICH_FIELDS)})")
    parser.add_argument("--include-p1", action="store_true",
                        help="Also include P1 leads in the outreach batch (default: P0 only)")
    parser.add_argument("--include-p2", action="store_true",
                        help="Also include P2 leads in the outreach batch (default: P0 only)")
    parser.add_argument("--skip-enrich", action="store_true",
                        help="Skip company enrichment step (Step 2)")
    parser.add_argument("--enrich-all", action="store_true",
                        help="Enrich every lead's company in Step 2, not just Decision Makers "
                             "(use when the sheet is all Champion-level contacts)")
    parser.add_argument("--with-competitors", action="store_true",
                        help="Opt in to the competitor lookup step (Step 5) — one extra web search "
                             "per company. Skipped by default; only run when competitor analysis is wanted.")
    parser.add_argument("--skip-posts", action="store_true",
                        help="Skip LinkedIn post scraping step (Step 6)")
    parser.add_argument("--skip-small-talk", action="store_true",
                        help="Skip small talk personalisation step (Step 7)")
    parser.add_argument("--skip-copy", action="store_true",
                        help="Skip LinkedIn copy writing step (Step 9)")
    args = parser.parse_args()

    client = anthropic.Anthropic()

    if args.skip_enrich:
        enrich_fields: List[Dict[str, str]] = []
    elif args.enrich_fields:
        wanted = {k.strip() for k in args.enrich_fields.split(",")}
        enrich_fields = [f for f in ENRICH_FIELDS if f["key"] in wanted]
    else:
        enrich_fields = list(ENRICH_FIELDS)

    # ------------------------------------------------------------------
    # Load context
    # ------------------------------------------------------------------
    print("\nLoading ICP context from context/...")
    icp_context = load_icp()
    post_config  = parse_post_config(icp_context)
    print(f"  Post scraping config: max_posts={post_config['max_posts']}, days_back={post_config['days_back']}")

    # ------------------------------------------------------------------
    # Pick a backend (Google Sheet or CSV) and read all rows
    # ------------------------------------------------------------------
    if args.sheet_id:
        backend = GoogleSheetsBackend(args.sheet_id, args.sheet_name)
        print(f"Reading sheet {args.sheet_id} tab '{args.sheet_name}'...")
    else:
        backend = CsvBackend(args.input_csv, args.output_csv)
        out = args.output_csv or args.input_csv
        print(f"Reading CSV {args.input_csv} (writing to {out})...")
    rows = backend.read_all()
    if not rows or len(rows) < 2:
        print("Sheet is empty or has no data rows. Exiting.")
        return

    headers: List[str] = list(rows[0])
    data_rows: List[List[str]] = rows[1:]
    if args.limit is not None:
        data_rows = data_rows[:max(0, args.limit)]
        print(f"--limit {args.limit}: processing first {len(data_rows)} lead(s).")
    print(f"Found {len(data_rows)} leads.")

    print("\nDetecting columns via Claude...")
    mapping = detect_columns(
        headers,
        data_rows[0] if data_rows else [],
        {
            # inputs
            "name":           "Full person name (combine first + last name columns if split)",
            "company":        "Current company name (not LinkedIn URL — just the name)",
            "linkedin":       "LinkedIn profile URL of the person (not the company's LinkedIn)",
            "position":       "Job title / role / position",
            # outputs (ok if missing — will be appended)
            "buyer_persona":  "Buyer-persona classification: Decision Maker / Champion / Non DM",
            "priority":       "Priority tier: P0 / P1 / P2",
            "reasoning":      "Reasoning text explaining the priority",
            "competitors":    "List of competitor companies of the lead's company",
            "company_url":         "Company website URL (homepage)",
            "company_linkedin":    "Company LinkedIn page URL",
            "company_description": "One-line description of what the company does",
            "employee_count":      "Headcount / number of employees at the company",
            "est_revenue":         "Estimated annual revenue of the company",
            "founded_year":        "Year company was founded",
            "total_funding":       "Total funding raised by the company",
            "hq":                  "HQ city",
            "icp_segment":         "ICP segment / tier the company belongs to",
            "post_links":     "LinkedIn post URLs of this person",
            "small_talk":     "Small-talk / personalisation details about the person",
            "hooks":          "Talking points / personalisation hooks for outreach",
            "copy":           "Final outreach message / copy",
        },
        client,
    )
    print(f"  Mapping: {mapping}")

    if not mapping.get("name") or not mapping.get("company") or not mapping.get("position"):
        print("ERROR: Could not detect required input columns from sheet.")
        print(f"Headers: {headers}")
        return

    leads: List[Dict] = []
    for row in data_rows:
        leads.append({
            "name":     cell_combined(row, mapping.get("name", [])),
            "company":  cell_combined(row, mapping.get("company", [])),
            "linkedin": cell_combined(row, mapping.get("linkedin", [])),
            "position": cell_combined(row, mapping.get("position", [])),
        })

    # ------------------------------------------------------------------
    # Pre-step: fill in missing name / position / company via web search
    # (rare; only fires for rows with one or more required fields blank)
    # ------------------------------------------------------------------
    incomplete = [i for i, l in enumerate(leads)
                  if not all((l.get(k) or "").strip() for k in ("name", "position", "company"))]
    if incomplete:
        print(f"\n--- Pre-step: Filling missing data for {len(incomplete)} lead(s) ---")
        for i in incomplete:
            before = {k: leads[i].get(k, "") for k in ("name", "position", "company")}
            find_missing_lead_data(leads[i], client)
            after = {k: leads[i].get(k, "") for k in ("name", "position", "company")}
            filled = {k: after[k] for k in after if not before[k] and after[k]}
            if filled:
                print(f"  Row {i+2}: filled {filled}")
            else:
                print(f"  Row {i+2}: nothing found")

    # ------------------------------------------------------------------
    # Step 1: Classify buyer personas
    # ------------------------------------------------------------------
    print("\n--- Step 1: Classifying buyer personas ---")
    if args.add_persona:
        print(f"  + Adding for this run: {args.add_persona}")
    if args.remove_persona:
        print(f"  - Removing for this run: {args.remove_persona}")

    classifications = classify_personas(leads, icp_context, args.add_persona, args.remove_persona, client)

    buyer_col_idx = get_or_create_col(headers, mapping, "buyer_persona", "Buyer Persona Match")
    backend.write_header(buyer_col_idx, headers[buyer_col_idx])
    backend.write_column(buyer_col_idx, classifications)

    dm_count    = sum(1 for c in classifications if c == "Decision Maker")
    champ_count = sum(1 for c in classifications if c == "Champion")
    ndm_count   = sum(1 for c in classifications if c == "Non Decision Maker")
    print(f"  Decision Makers: {dm_count} | Champions: {champ_count} | Non DMs: {ndm_count}")
    print(f"  Written to column {col_letter(buyer_col_idx)}")

    # ------------------------------------------------------------------
    # Step 2: Enrich company firmographics
    # ------------------------------------------------------------------
    # By default only Decision Makers' companies are enriched (cost control).
    # --enrich-all widens this to every lead, for sheets that are all
    # Champion-level contacts where scoring still needs firmographics.
    if args.enrich_all:
        dm_indices = list(range(len(classifications)))
    else:
        dm_indices = [i for i, c in enumerate(classifications) if c == "Decision Maker"]

    if dm_indices and enrich_fields:
        label = "company(ies)" if args.enrich_all else "Decision Maker company(ies)"
        print(f"\n--- Step 2: Enriching {len(dm_indices)} {label} ---")
        print(f"  Fields: {', '.join(f['key'] for f in enrich_fields)}")

        # For each field: locate (or create) the destination column on the sheet.
        col_idx_by_key: Dict[str, int] = {}
        for f in enrich_fields:
            idx = get_or_create_col(headers, mapping, f["key"], f["label"])
            col_idx_by_key[f["key"]] = idx
            backend.write_header(idx, headers[idx])

        def _all_filled(row) -> bool:
            return all(
                col_idx_by_key[f["key"]] < len(row) and row[col_idx_by_key[f["key"]]].strip()
                for f in enrich_fields
            )

        # Enrich each UNIQUE company that still needs it, concurrently.
        # CRASH-SAFETY: each result is appended to a local JSONL checkpoint the
        # instant it comes back (instant + free — no Sheets call per company).
        # If the run dies midway (crash, credit-exhaustion, Ctrl-C), the
        # checkpoint still holds every company done so far; a re-run loads it,
        # skips those, and only pays for what's missing. The Sheet is flushed at
        # 25/50/75/100% milestones during the run (see _flush_enrich below).
        ck_id = args.sheet_id or (args.input_csv or "csv")
        ck_path = checkpoint_path(f"linkedin_outreach_enrich_{ck_id}")
        company_cache: Dict[str, Dict[str, str]] = {
            k: v for k, v in checkpoint_load(ck_path).items() if isinstance(v, dict)
        }
        if company_cache:
            print(f"  Resuming from checkpoint: {len(company_cache)} company(ies) already enriched.")

        rows_by_company: Dict[str, List[int]] = {}
        to_enrich: List[str] = []
        seen_companies: set = set()
        for i in dm_indices:
            company = leads[i]["company"]
            if not company:
                continue
            if _all_filled(data_rows[i]):
                enriched = {f["key"]: cell(data_rows[i], col_idx_by_key[f["key"]]) for f in enrich_fields}
                _apply_enrichment(leads[i], enriched)
                continue
            rows_by_company.setdefault(company, []).append(i)
            # Skip companies already in the checkpoint — never re-pay for them.
            if company not in seen_companies and company not in company_cache:
                seen_companies.add(company)
                to_enrich.append(company)

        # Push enrichment to the Sheet as one batched per-field column write,
        # reconstructed from the checkpoint cache + existing cells so nothing
        # else is clobbered. Fired at 25/50/75/100% milestones DURING the run
        # (halves for <=200 companies) — not just at the very end — so a long
        # enrich is durable in the Sheet even if the local checkpoint is lost or
        # the process dies late.
        def _flush_enrich(done: Optional[int] = None) -> None:
            if done is not None:
                print(f"  Flushing enrichment to the sheet ({done}/{len(to_enrich)} companies)...")
            for f in enrich_fields:
                col_idx = col_idx_by_key[f["key"]]
                column: List[str] = []
                for i, row in enumerate(data_rows):
                    company = leads[i]["company"] if i < len(leads) else ""
                    if company in company_cache:
                        column.append(company_cache[company].get(f["key"], "") or cell(row, col_idx))
                    else:
                        column.append(cell(row, col_idx))  # preserve whatever's there
                backend.write_column(col_idx, column)

        flusher = MilestoneFlusher(len(to_enrich), _flush_enrich)

        def _persist(_idx, company, enriched, err):
            """Main-thread callback: checkpoint one company's firmographics to
            local disk the instant it finishes, then flush the Sheet on milestones."""
            if err:
                print(f"    ! {company} enrichment failed: {err}")
            enriched = enriched or {}
            company_cache[company] = enriched
            checkpoint_append(ck_path, company, enriched)
            flusher.tick()

        if to_enrich:
            print(f"  Enriching {len(to_enrich)} unique company(ies) — checkpointing each to {ck_path} ...")
            map_rate_limited(
                lambda c: enrich_company(c, enrich_fields, client),
                to_enrich, max_workers=ENRICH_CONCURRENCY, on_result=_persist,
            )
        else:
            # All companies were already cached — nothing runs this pass, but
            # still write the columns from cache so the Sheet reflects it.
            _flush_enrich()

        # Apply enrichment to every lead in memory (for scoring / copy downstream).
        for i in dm_indices:
            company = leads[i]["company"]
            if company in company_cache:
                _apply_enrichment(leads[i], company_cache[company])

    elif not enrich_fields:
        print("\n--- Step 2: Skipping enrichment (--skip-enrich) ---")
    else:
        print("\n--- Step 2: No Decision Makers — skipping enrichment ---")

    # ------------------------------------------------------------------
    # Step 3: Score all leads P0 / P1 / P2
    # ------------------------------------------------------------------
    print("\n--- Step 3: Scoring all leads P0 / P1 / P2 ---")
    scores = score_leads(leads, classifications, icp_context, client)

    priority_col_idx  = get_or_create_col(headers, mapping, "priority",    "Priority")
    icp_col_idx       = get_or_create_col(headers, mapping, "icp_segment", "ICP Segment")
    reasoning_col_idx = get_or_create_col(headers, mapping, "reasoning",   "Reasoning")

    backend.write_header(priority_col_idx,  headers[priority_col_idx])
    backend.write_header(icp_col_idx,       headers[icp_col_idx])
    backend.write_header(reasoning_col_idx, headers[reasoning_col_idx])
    backend.write_column(priority_col_idx,  [s["priority"] for s in scores])
    backend.write_column(icp_col_idx,       [s.get("icp_segment", "") for s in scores])
    backend.write_column(reasoning_col_idx, [s["reasoning"] for s in scores])

    p0 = sum(1 for s in scores if s["priority"] == "P0")
    p1 = sum(1 for s in scores if s["priority"] == "P1")
    p2 = sum(1 for s in scores if s["priority"] == "P2")
    print(f"  P0: {p0} | P1: {p1} | P2: {p2}")
    print(f"  Priority col {priority_col_idx} | ICP Segment col {icp_col_idx} | Reasoning col {reasoning_col_idx}")

    # ------------------------------------------------------------------
    # Step 4: Select outreach batch (P0 only by default; opt-in via --include-p1/p2)
    # ------------------------------------------------------------------
    print("\n--- Step 4: Selecting outreach batch ---")
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

    # ------------------------------------------------------------------
    # In-memory caches threaded across the remaining steps
    # ------------------------------------------------------------------
    competitors_by_lead: Dict[int, List[str]] = {}  # populated in Step 5
    post_data_by_lead:   Dict[int, List[Dict]] = {}  # populated in Step 6
    small_talk_by_lead:  Dict[int, str]        = {}  # populated in Step 7
    hooks_by_lead:       Dict[int, str]        = {}  # populated in Step 8

    # ------------------------------------------------------------------
    # Step 5: Find competitors for each filtered lead's company
    # ------------------------------------------------------------------
    if not args.with_competitors:
        print("\n--- Step 5: Skipping competitor lookup (default — pass --with-competitors to enable) ---")
    else:
        print(f"\n--- Step 5: Finding competitors for {len(outreach_indices)} leads ---")

        competitors_col_idx = get_or_create_col(headers, mapping, "competitors", "Competitors")
        backend.write_header(competitors_col_idx, headers[competitors_col_idx])

        # Look up competitors for each UNIQUE company in the batch, in parallel.
        competitor_cache: Dict[str, List[str]] = {}
        uniq_companies: List[str] = []
        seen_companies = set()
        for i in outreach_indices:
            c = leads[i]["company"]
            if c and c not in seen_companies:
                seen_companies.add(c)
                uniq_companies.append(c)

        results, errors = map_rate_limited(
            lambda c: find_competitors(c, client), uniq_companies, max_workers=ENRICH_CONCURRENCY,
        )
        for c, r, e in zip(uniq_companies, results, errors):
            if e:
                print(f"  {c} — competitor lookup failed: {e}")
            competitor_cache[c] = r or []

        # Write per lead sequentially.
        for i in outreach_indices:
            comps = competitor_cache.get(leads[i]["company"], [])
            competitors_by_lead[i] = comps  # keep in memory for Step 9
            backend.write_cell(i + 2, competitors_col_idx, ", ".join(comps))
            if comps:
                print(f"  {leads[i]['company']} → {', '.join(comps)}")

    # ------------------------------------------------------------------
    # Step 6: Scrape + filter LinkedIn posts
    # ------------------------------------------------------------------
    if args.skip_posts:
        print("\n--- Step 6: Skipping post scraping (--skip-posts) ---")
    else:
        has_linkedin = [i for i in outreach_indices if (leads[i].get("linkedin") or "").strip()]
        print(f"\n--- Step 6: Scraping LinkedIn posts for {len(has_linkedin)} leads ---")
        print(f"  Config: {post_config['max_posts']} posts / {post_config['days_back']} days")

        post_links_col_idx = get_or_create_col(headers, mapping, "post_links", "LinkedIn Post Links")
        backend.write_header(post_links_col_idx, headers[post_links_col_idx])

        # Leads without a LinkedIn URL get an empty entry so Step 8 can read it.
        for i in outreach_indices:
            if i not in has_linkedin:
                post_data_by_lead[i] = []

        # Checkpoint Apify post results per LinkedIn URL — a crash mid-scrape
        # keeps everything already pulled; a re-run skips those profiles.
        posts_ck = checkpoint_path(f"linkedin_outreach_posts_{args.sheet_id or (args.input_csv or 'csv')}")
        posts_done = checkpoint_load(posts_ck)
        pending_posts = []
        for i in has_linkedin:
            url = leads[i]["linkedin"]
            if url in posts_done and isinstance(posts_done[url], dict):
                r = posts_done[url]
                post_data_by_lead[i] = r.get("posts_data", [])
                backend.write_cell(i + 2, post_links_col_idx, "\n".join(r.get("urls", [])))
            else:
                pending_posts.append(i)
        if posts_done:
            print(f"  Resuming from checkpoint: {len(has_linkedin) - len(pending_posts)} profile(s) already scraped.")

        def _persist_posts(_idx, i, r, e):
            if e or not r:
                if e:
                    print(f"  {leads[i]['name']} — post scraping failed: {e}")
                post_data_by_lead[i] = []
                return
            post_data_by_lead[i] = r["posts_data"]  # full text kept in memory
            checkpoint_append(posts_ck, leads[i]["linkedin"], {"urls": r["urls"], "posts_data": r["posts_data"]})
            backend.write_cell(i + 2, post_links_col_idx, "\n".join(r["urls"]) if r["urls"] else "")
            print(f"  {leads[i]['name']} ({leads[i]['company']}) → {len(r['urls'])} post(s) matched")

        # profile-posts actor throttles on bursts — space starts POSTS_MIN_INTERVAL
        # apart, overlap run-times across a small pool.
        map_rate_limited(
            lambda i: scrape_and_filter_posts(
                profile_url=leads[i]["linkedin"], icp_context=icp_context,
                max_posts=post_config["max_posts"], days_back=post_config["days_back"], client=client,
            ),
            pending_posts, min_interval=POSTS_MIN_INTERVAL, max_workers=POSTS_CONCURRENCY,
            on_result=_persist_posts,
        )

    # ------------------------------------------------------------------
    # Step 7: Small talk personalisation
    # ------------------------------------------------------------------
    if args.skip_small_talk:
        print("\n--- Step 7: Skipping small talk (--skip-small-talk) ---")
    elif not _SMALL_TALK_AVAILABLE:
        print("\n--- Step 7: Small Talk Scraper failed to import — skipping ---")
    else:
        print(f"\n--- Step 7: Gathering small talk details for {len(outreach_indices)} leads ---")

        small_talk_col_idx = get_or_create_col(headers, mapping, "small_talk", "Small Talk")
        backend.write_header(small_talk_col_idx, headers[small_talk_col_idx])

        all_st = [i for i in outreach_indices if (leads[i].get("name") or "").strip()]
        # Checkpoint small-talk results (Apify) per LinkedIn URL / name key.
        st_ck = checkpoint_path(f"linkedin_outreach_smalltalk_{args.sheet_id or (args.input_csv or 'csv')}")
        st_done = checkpoint_load(st_ck)
        st_indices = []
        for i in all_st:
            key = leads[i].get("linkedin") or f"{leads[i]['name']}|{leads[i]['company']}"
            if key in st_done:
                detail = st_done[key] or ""
                small_talk_by_lead[i] = detail
                backend.write_cell(i + 2, small_talk_col_idx, detail)
            else:
                st_indices.append(i)
        if st_done:
            print(f"  Resuming from checkpoint: {len(all_st) - len(st_indices)} lead(s) already done.")

        def _persist_st(_idx, i, r, e):
            detail = "" if e else (r or "")
            if e:
                print(f"  {leads[i]['name']} — small talk failed: {e}")
            small_talk_by_lead[i] = detail  # keep in memory for Step 8
            key = leads[i].get("linkedin") or f"{leads[i]['name']}|{leads[i]['company']}"
            checkpoint_append(st_ck, key, detail)
            backend.write_cell(i + 2, small_talk_col_idx, detail)
            print(f"  {leads[i]['name']} ({leads[i]['company']}) → {(detail[:80] + '...') if len(detail) > 80 else (detail or '(none)')}")

        map_rate_limited(
            lambda i: scrape_small_talk(
                profile_url=leads[i].get("linkedin", ""), name=leads[i]["name"], company=leads[i]["company"],
            ),
            st_indices, max_workers=ENRICH_CONCURRENCY, on_result=_persist_st,
        )

    # ------------------------------------------------------------------
    # Step 8: Personalisation hooks
    # ------------------------------------------------------------------
    if args.skip_small_talk and args.skip_posts:
        # Both data sources were skipped — nothing to personalise from
        print("\n--- Step 8: Skipping personalisation hooks (no post or small talk data) ---")
    elif not _PERSONALISATION_AVAILABLE:
        print("\n--- Step 8: Personalisation Hook Skill failed to import — skipping ---")
    else:
        print(f"\n--- Step 8: Generating personalisation hooks for {len(outreach_indices)} leads ---")

        hooks_col_idx = get_or_create_col(headers, mapping, "hooks", "Personalisation Hook")
        backend.write_header(hooks_col_idx, headers[hooks_col_idx])

        # Checkpoint each hook + write it to the sheet the instant it's done, so a
        # crash mid-run keeps every hook already generated (never re-paid) and
        # leaves it in the sheet — no end-only batched write left to lose.
        hooks_ck = checkpoint_path(f"linkedin_outreach_hooks_{args.sheet_id or (args.input_csv or 'csv')}")
        hooks_done = checkpoint_load(hooks_ck)
        hook_indices = []
        for i in outreach_indices:
            key = leads[i].get("linkedin") or f"{leads[i]['name']}|{leads[i]['company']}"
            if key in hooks_done:
                hooks = hooks_done[key] or ""
                hooks_by_lead[i] = hooks
                backend.write_cell(i + 2, hooks_col_idx, hooks)
            else:
                hook_indices.append(i)
        if hooks_done:
            print(f"  Resuming from checkpoint: {len(outreach_indices) - len(hook_indices)} lead(s) already done.")

        def _hook_task(i: int) -> str:
            lead = leads[i]
            return generate_personalisation_hooks(
                name=lead["name"], company=lead["company"], position=lead["position"],
                matching_posts=post_data_by_lead.get(i, []),
                small_talk=small_talk_by_lead.get(i, ""),
                icp_context=icp_context,
                competitors=competitors_by_lead.get(i, []),
                company_description=lead.get("company_description", ""),
                employee_count=lead.get("employee_count", ""),
                est_revenue=lead.get("est_revenue", ""),
                total_funding=lead.get("total_funding", ""),
                hq=lead.get("hq", ""),
            )

        def _persist_hook(_idx, i, r, e):
            hooks = "" if e else (r or "")
            if e:
                print(f"  {leads[i]['name']} — hook generation failed: {e}")
            hooks_by_lead[i] = hooks  # keep in memory for Step 9
            key = leads[i].get("linkedin") or f"{leads[i]['name']}|{leads[i]['company']}"
            checkpoint_append(hooks_ck, key, hooks)
            backend.write_cell(i + 2, hooks_col_idx, hooks)
            print(f"  {leads[i]['name']} ({leads[i]['company']}) → {(hooks[:100] + '...') if len(hooks) > 100 else (hooks or '(none)')}")

        map_rate_limited(_hook_task, hook_indices, max_workers=ENRICH_CONCURRENCY, on_result=_persist_hook)

    # ------------------------------------------------------------------
    # Step 9: Write LinkedIn copy
    # ------------------------------------------------------------------
    if args.skip_copy:
        print("\n--- Step 9: Skipping LinkedIn copy (--skip-copy) ---")
    elif not _COPY_WRITER_AVAILABLE:
        print("\n--- Step 9: LinkedIn Copy Writer Skill failed to import — skipping ---")
    else:
        print(f"\n--- Step 9: Writing LinkedIn copy for {len(outreach_indices)} leads ---")

        copy_col_idx = get_or_create_col(headers, mapping, "copy", "LinkedIn Copy")
        backend.write_header(copy_col_idx, headers[copy_col_idx])

        # Checkpoint each finished message + write it to the sheet immediately, so
        # a crash never re-pays for copy already written and never loses it to an
        # end-only batched write.
        copy_ck = checkpoint_path(f"linkedin_outreach_copy_{args.sheet_id or (args.input_csv or 'csv')}")
        copy_done = checkpoint_load(copy_ck)
        copy_indices = []
        for i in outreach_indices:
            key = leads[i].get("linkedin") or f"{leads[i]['name']}|{leads[i]['company']}"
            if key in copy_done:
                backend.write_cell(i + 2, copy_col_idx, copy_done[key] or "")
            else:
                copy_indices.append(i)
        if copy_done:
            print(f"  Resuming from checkpoint: {len(outreach_indices) - len(copy_indices)} lead(s) already done.")

        def _copy_task(i: int) -> str:
            lead = leads[i]
            return write_linkedin_copy(
                name=lead["name"], company=lead["company"], position=lead["position"],
                buyer_persona=classifications[i],
                priority=scores[i]["priority"],
                competitors=competitors_by_lead.get(i, []),
                matching_posts=post_data_by_lead.get(i, []),
                small_talk=small_talk_by_lead.get(i, ""),
                personalisation_hook=hooks_by_lead.get(i, ""),
                icp_context=icp_context,
                employee_count=lead.get("employee_count", ""),
                est_revenue=lead.get("est_revenue", ""),
                total_funding=lead.get("total_funding", ""),
                hq=lead.get("hq", ""),
            )

        def _persist_copy(_idx, i, r, e):
            copy = "" if e else (r or "")
            if e:
                print(f"  {leads[i]['name']} — copy generation failed: {e}")
            key = leads[i].get("linkedin") or f"{leads[i]['name']}|{leads[i]['company']}"
            checkpoint_append(copy_ck, key, copy)
            backend.write_cell(i + 2, copy_col_idx, copy)
            print(f"  {leads[i]['name']} ({leads[i]['company']}) → {(copy[:100] + '...') if len(copy) > 100 else (copy or '(none)')}")

        map_rate_limited(_copy_task, copy_indices, max_workers=ENRICH_CONCURRENCY, on_result=_persist_copy)

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    print("\n======= Done =======")
    print(f"  Total leads processed:  {len(leads)}")
    print(f"  Buyer personas:         {dm_count} DMs | {champ_count} Champions | {ndm_count} Non DMs")
    print(f"  Priority scores:        {p0} P0 | {p1} P1 | {p2} P2")
    print(f"  Outreach batch:         {len(outreach_indices)} leads")


if __name__ == "__main__":
    main()
