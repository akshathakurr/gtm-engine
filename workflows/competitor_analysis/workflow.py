"""
Competitor Analysis Workflow — Deep competitive intelligence per competitor.

Reads competitor names + URLs from the 'Bird Eye' sheet tab and fills all columns.
Other tabs (Company, Founder, GTM, Product) auto-populate via sheet formulas.

Steps per competitor:
  1  — Website scrape (free — done first, reused across all later steps)
  2  — Company LinkedIn URL (from scrape, web search fallback)
  3  — Company Description (one-liner from scrape)
  4  — Firmographics via Exa (headcount exact, year, funding stage, total funding, revenue, HQ city)
  5  — Recent news via Exa (one-liner + source URL per item)
  6  — Founders (name, LinkedIn, Twitter) via web search
  7  — Founder post types via LinkedIn post scraper + Twitter scraper
  8  — Product info from website scrape (persona, sales motion, CTA, pricing, features, logos, messaging, SEO)
  9  — Customer reviews via Review Scraper (G2 → Trustpilot fallback)
  10 — Deal size via web search
  11 — Content type synthesis from founder posts + company blog
  12 — Final Claude analysis (Target ICP categorized, Competitor Score /5, Strengths, Weaknesses)

Usage:
  python -m workflows.competitor_analysis.workflow --sheet-id SHEET_ID
  python -m workflows.competitor_analysis.workflow --sheet-id SHEET_ID --sheet-name "Bird Eye"
  python -m workflows.competitor_analysis.workflow --input-csv competitors.csv
  python -m workflows.competitor_analysis.workflow --sheet-id SHEET_ID --skip-reviews --skip-twitter
  python -m workflows.competitor_analysis.workflow --sheet-id SHEET_ID --auto       # CI / cron
"""

import os
import sys
import time
import argparse
import re
from typing import List, Dict

import anthropic

from config import CONTEXT_DIR
from workflows._common import (
    GoogleSheetsBackend, CsvBackend, find_col, ensure_col, cell, load_icp,
)
from workflows.competitor_analysis.steps import (
    _run_parallel,
    scrape_website, find_linkedin_url, draft_description, get_firmographics,
    get_recent_news, find_founders, get_founder_post_type, extract_product_info,
    get_customer_reviews, get_deal_size, get_content_type, analyze_competitor,
)


# ---------------------------------------------------------------------------
# Empty-context guard — flag missing context.md sections before doing work
# ---------------------------------------------------------------------------

# Sections in context.md that materially affect the final analysis prompt.
# If any are missing/empty, the LLM scoring/strength/weakness output gets vague.
RECOMMENDED_CONTEXT_SECTIONS = [
    "Product",
    "Ideal Customer Profile",
    "Competitors",
]


def _section_body(text: str, header: str) -> str:
    if not text:
        return ""
    pattern = rf"(?ms)^##\s+{re.escape(header)}\s*$\n(.*?)(?=^##\s+|\Z)"
    m = re.search(pattern, text)
    if not m:
        return ""
    section = m.group(1)
    ans = re.search(r"(?ms)^###\s+Answer\s*$\n(.*?)(?=^###\s+|\Z)", section)
    body = ans.group(1) if ans else section
    body = "\n".join(ln for ln in body.splitlines() if not ln.strip().startswith("<!--")).strip()
    if body.lower() in ("(fill this in)", "(none)", "(skip)"):
        return ""
    # Treat a body that's only template scaffolding ("- **X:**" with no values) as empty
    stripped = "\n".join(
        ln for ln in body.splitlines()
        if ln.strip() and not re.match(r"^[-*\s]*\*\*[^*]+:\*\*\s*$", ln.strip())
    ).strip()
    return stripped


def check_context_complete(auto: bool) -> None:
    """Warn (or under --auto, abort) if recommended context.md sections are empty."""
    path = os.path.join(CONTEXT_DIR, "context.md")
    text = ""
    if os.path.exists(path):
        with open(path) as f:
            text = f.read()

    missing = [h for h in RECOMMENDED_CONTEXT_SECTIONS if not _section_body(text, h)]
    if not missing:
        return

    print("\n" + "=" * 70)
    print(" Heads up — context/context.md is missing (or empty for) these sections:")
    print("=" * 70)
    for h in missing:
        print(f"  · ## {h}")
    print("\nThese power the final competitor analysis (score, strengths, weaknesses,")
    print("Target ICP). Without them the analysis will be vague and generic.")
    print("See context/context.md.example for the full template.\n")

    if auto:
        print("ERROR: --auto specified — aborting. Fill those sections and re-run.")
        sys.exit(2)

    choice = input("Continue anyway? [y/N] ").strip().lower()
    if choice not in ("y", "yes"):
        print("Aborted. Fill context/context.md and re-run.")
        sys.exit(0)
    print()


# ---------------------------------------------------------------------------
# Column definitions — exact Bird Eye header names (Notes is manual, excluded)
# ---------------------------------------------------------------------------
OUTPUT_COLUMNS = [
    "Company LinkedIn URL",
    "Company Description",
    "Competitor Score",
    "Strength",
    "Weakness",
    "Employee Count",
    "Founded Year",
    "Last Funding Stage",
    "Total Funding",
    "Est. Revenue",
    "HQ Location",
    "Recent News",
    "Founder (1) Name",
    "Founder (1) LinkedIn",
    "Founder (1) Twitter",
    "Founder (1) Post type",
    "Founder (2) Name",
    "Founder (2) LinkedIn",
    "Founder (2) Twitter",
    "Founder (2) Post type",
    "Target Persona (User)",
    "Primary CTA",
    "Pricing",
    "Customer Stories",
    "Product Features",
    "Customer Reviews",
    "Target ICP",
    "Sales Motion",
    "Deal Size",
    "Top Logos",
    "Marketing Messaging",
    "Content Type",
    "SEO",
]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Competitor Analysis — deep competitive intelligence")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--sheet-id",             default=None, help="Google Sheet ID")
    src.add_argument("--input-csv",            default=None, help="Read competitors from a CSV file")
    parser.add_argument("--sheet-name",        default="Bird Eye", help="(Sheets only) tab name")
    parser.add_argument("--output-csv",        default=None,
                        help="(CSV only) Where to write output. Defaults to overwriting --input-csv.")
    parser.add_argument("--only",              default="", help="Only process this competitor name (exact match)")
    parser.add_argument("--skip-reviews",      action="store_true", help="Skip G2/Trustpilot scraping")
    parser.add_argument("--skip-twitter",      action="store_true", help="Skip Twitter scraping (saves Apify credits)")
    parser.add_argument("--skip-founder-posts",action="store_true", help="Skip all founder post scraping")
    parser.add_argument("--skip-analysis",     action="store_true", help="Skip final scoring and analysis")
    parser.add_argument("--auto",              action="store_true",
                        help="Run non-interactively. Errors out if context.md is missing required sections.")
    args = parser.parse_args()

    # Pre-step: warn (or abort under --auto) if context.md is too sparse for the analysis to be meaningful.
    check_context_complete(auto=args.auto)

    client = anthropic.Anthropic()

    print("\nLoading ICP context from context/...")
    icp_context = load_icp()

    if args.sheet_id:
        backend = GoogleSheetsBackend(args.sheet_id, args.sheet_name)
        source_label = f"sheet tab '{args.sheet_name}'"
    else:
        backend = CsvBackend(args.input_csv, args.output_csv)
        source_label = f"CSV '{args.input_csv}'"

    print(f"Reading {source_label}...")
    rows = backend.read_all()
    if not rows or len(rows) < 2:
        print("Source empty or no data rows. Exiting.")
        return

    headers:   List[str]       = list(rows[0])
    data_rows: List[List[str]] = rows[1:]
    print(f"Found {len(data_rows)} competitors.")

    name_col = find_col(headers, "Company Name", "Name")
    url_col  = find_col(headers, "Company URL", "URL", "Website")
    if name_col is None:
        print(f"ERROR: No name column found. Headers: {headers}")
        return

    # Ensure all output columns exist, write headers once
    col_map: Dict[str, int] = {}
    for col_name in OUTPUT_COLUMNS:
        idx = ensure_col(headers, col_name)
        col_map[col_name] = idx
        backend.write_header(idx, col_name)

    # ---------------------------------------------------------------------------
    for i, row in enumerate(data_rows):
        competitor = cell(row, name_col)
        if not competitor:
            continue
        if args.only and competitor.lower() != args.only.lower():
            continue

        website = cell(row, url_col) if url_col is not None else ""
        row_num = i + 2

        print(f"\n{'='*60}")
        print(f"[{i+1}/{len(data_rows)}] {competitor}  ({website or 'no URL'})")
        print(f"{'='*60}")

        # Track values written this run so the analysis step can use them
        written: Dict[str, str] = {}

        def get_col(name: str) -> str:
            """Read from original row OR from this run's writes."""
            return written.get(name) or (
                row[col_map[name]].strip()
                if col_map.get(name) is not None and col_map[name] < len(row)
                else ""
            )

        def write_col(name: str, value: str) -> None:
            if value:
                idx = col_map[name]
                backend.write_cell(row_num, idx, value)
                written[name] = value

        # ── Step 1: Website scrape ────────────────────────────────────────────
        scraped: dict = {}
        if website:
            print("  [1] Scraping website...")
            scraped = scrape_website(website)
            pages = len(scraped.get("full_text_by_page", {}))
            print(f"    → {pages} pages scraped")
        else:
            print("  [1] No website URL — skipping scrape")

        # ── Steps 2–10: independent enrichment ────────────────────────────────
        # These depend only on the company name / website / scraped pages, not on
        # each other, and use only Exa + Claude (plus the reviews actor — a single
        # call to a different actor than the founder-post step). Compute them
        # concurrently, then write sequentially so the backend stays single-threaded.
        firm_cols = ["Employee Count", "Founded Year", "Last Funding Stage", "Total Funding", "Est. Revenue", "HQ Location"]
        f1_cols = ["Founder (1) Name", "Founder (1) LinkedIn", "Founder (1) Twitter"]
        f2_cols = ["Founder (2) Name", "Founder (2) LinkedIn", "Founder (2) Twitter"]
        product_cols = [
            "Target Persona (User)", "Sales Motion", "Primary CTA", "Pricing",
            "Customer Stories", "Product Features", "Top Logos", "Marketing Messaging", "SEO",
        ]
        firm_missing     = [c for c in firm_cols if not get_col(c)]
        missing_founders = [c for c in f1_cols + f2_cols if not get_col(c)]
        product_missing  = [c for c in product_cols if not get_col(c)] if scraped else []

        tasks: Dict[str, "callable"] = {}
        if not get_col("Company LinkedIn URL"):
            tasks["li_url"] = lambda: find_linkedin_url(competitor, website, client)
        if not get_col("Company Description") and scraped:
            tasks["desc"] = lambda: draft_description(competitor, scraped, client)
        if firm_missing:
            tasks["firm"] = lambda: get_firmographics(competitor, client)
        if not get_col("Recent News"):
            tasks["news"] = lambda: get_recent_news(competitor, website, client)
        if missing_founders:
            tasks["founders"] = lambda: find_founders(competitor, website, client)
        if product_missing:
            tasks["prod"] = lambda: extract_product_info(competitor, scraped, client)
        if not get_col("Deal Size"):
            tasks["deal"] = lambda: get_deal_size(competitor, client)
        if not args.skip_reviews and not get_col("Customer Reviews"):
            tasks["reviews"] = lambda: get_customer_reviews(competitor, website, client)

        if tasks:
            print(f"  [2-10] Enriching — {len(tasks)} lookups in parallel: {', '.join(tasks)}")
            res = _run_parallel(tasks)
            for key, (_v, err) in res.items():
                if err:
                    print(f"    ! {key} lookup failed: {err}")
        else:
            res = {}
            print("  [2-10] All enrichment columns already filled — skipping")

        def _val(key, default):
            value, _err = res.get(key, (None, None))
            return value if value is not None else default

        # Step 2: Company LinkedIn URL
        if "li_url" in tasks:
            write_col("Company LinkedIn URL", _val("li_url", ""))

        # Step 3: Company description
        if "desc" in tasks:
            write_col("Company Description", _val("desc", ""))

        # Step 4: Firmographics
        if "firm" in tasks:
            firm = _val("firm", {}) or {}
            for c in firm_missing:
                write_col(c, firm.get(c, ""))

        # Step 5: Recent news
        if "news" in tasks:
            write_col("Recent News", _val("news", ""))

        # Step 6: Founders (write, then rebuild the list from final cell values)
        if "founders" in tasks:
            found = _val("founders", []) or []
            f1 = found[0] if len(found) > 0 else {}
            f2 = found[1] if len(found) > 1 else {}
            for col_name, key in [("Founder (1) Name", "name"), ("Founder (1) LinkedIn", "linkedin"), ("Founder (1) Twitter", "twitter")]:
                if not get_col(col_name): write_col(col_name, f1.get(key, ""))
            for col_name, key in [("Founder (2) Name", "name"), ("Founder (2) LinkedIn", "linkedin"), ("Founder (2) Twitter", "twitter")]:
                if not get_col(col_name): write_col(col_name, f2.get(key, ""))
        founders: List[Dict[str, str]] = [
            {"name": get_col("Founder (1) Name"), "linkedin": get_col("Founder (1) LinkedIn"), "twitter": get_col("Founder (1) Twitter")},
            {"name": get_col("Founder (2) Name"), "linkedin": get_col("Founder (2) LinkedIn"), "twitter": get_col("Founder (2) Twitter")},
        ]

        # Step 8: Product info
        if "prod" in tasks:
            prod = _val("prod", {}) or {}
            for c in product_missing:
                write_col(c, prod.get(c, ""))

        # Step 9: Customer reviews
        if "reviews" in tasks:
            review_val = (_val("reviews", "") or "") or "not available"
            idx = col_map["Customer Reviews"]
            backend.write_cell(row_num, idx, review_val)
            written["Customer Reviews"] = review_val
        elif args.skip_reviews:
            print("  [9] Skipping reviews (--skip-reviews)")

        # Step 10: Deal size
        if "deal" in tasks:
            write_col("Deal Size", _val("deal", ""))

        # ── Step 7: Founder post types (throttled actors → kept sequential) ────
        founder_post_summaries: List[str] = []
        if args.skip_founder_posts:
            print("  [7] Skipping founder posts (--skip-founder-posts)")
        else:
            for fi, (founder, post_col) in enumerate(zip(
                founders[:2],
                ["Founder (1) Post type", "Founder (2) Post type"],
            )):
                fname = founder.get("name", "") if founder else ""
                if not fname:
                    continue
                if get_col(post_col):
                    founder_post_summaries.append(f"[{fname}] {get_col(post_col)}")
                    continue
                print(f"  [7.{fi+1}] Getting post type for {fname}...")
                pt = get_founder_post_type(
                    fname,
                    founder.get("linkedin", ""),
                    founder.get("twitter", ""),
                    client,
                    skip_twitter=args.skip_twitter,
                )
                write_col(post_col, pt)
                if pt:
                    founder_post_summaries.append(f"[{fname}] {pt}")
                print(f"    → {pt[:100] or '(no data)'}")
                time.sleep(2)

        # ── Step 11: Content type ─────────────────────────────────────────────
        if not get_col("Content Type"):
            print("  [11] Synthesising content type...")
            ct = get_content_type(competitor, founder_post_summaries, scraped, client)
            write_col("Content Type", ct)
            print(f"    → {ct[:100] or '(none)'}")
        else:
            print("  [11] Content Type already filled — skipping")

        # ── Step 12: Final analysis ───────────────────────────────────────────
        if args.skip_analysis:
            print("  [12] Skipping analysis (--skip-analysis)")
        else:
            analysis_cols = ["Competitor Score", "Strength", "Weakness", "Target ICP"]
            missing_analysis = [c for c in analysis_cols if not get_col(c)]
            if missing_analysis:
                print("  [12] Running final analysis...")
                profile = {c: get_col(c) for c in OUTPUT_COLUMNS if get_col(c)}
                analysis = analyze_competitor(competitor, profile, icp_context, client)
                for c in missing_analysis:
                    write_col(c, analysis.get(c, ""))
                print(f"    → Score: {analysis.get('Competitor Score','?')} | ICP: {analysis.get('Target ICP','?')}")
                print(f"    → Strength:  {analysis.get('Strength','')[:80]}")
                print(f"    → Weakness:  {analysis.get('Weakness','')[:80]}")
            else:
                print("  [12] Analysis columns already filled — skipping")

    print(f"\n{'='*60}")
    print(f"Done. Processed {len([r for r in data_rows if cell(r, name_col)])} competitors.")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
