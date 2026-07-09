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
import argparse
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict

import anthropic

from config import CONTEXT_DIR
from workflows._common import (
    GoogleSheetsBackend, CsvBackend, find_col, ensure_col, cell, load_icp,
    RateLimiter,
)
from workflows.competitor_analysis.steps import (
    OUTPUT_COLUMNS, COMPANY_CONCURRENCY, APIFY_MIN_INTERVAL, process_competitor,
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
    parser.add_argument("--concurrency",       type=int, default=COMPANY_CONCURRENCY, metavar="N",
                        help=f"Number of competitors to process in parallel (default: {COMPANY_CONCURRENCY}). Use 1 for fully sequential.")
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
    # Build the work list — skip blank-name rows and, with --only, non-matches.
    work = []
    for i, row in enumerate(data_rows):
        competitor = cell(row, name_col)
        if not competitor:
            continue
        if args.only and competitor.lower() != args.only.lower():
            continue
        website = cell(row, url_col) if url_col is not None else ""
        work.append((i, competitor, website, row))

    if not work:
        print("No competitors to process.")
        return

    total = len(data_rows)
    concurrency = max(1, min(args.concurrency, len(work)))
    # One shared limiter so the throttled Apify actors (founder posts + reviews)
    # are spaced across ALL concurrent companies, not just within one.
    apify_limiter = RateLimiter(APIFY_MIN_INTERVAL)

    print(f"\nProcessing {len(work)} competitor(s), {concurrency} at a time...")

    # Each competitor is enriched off the main thread (compute only — no backend
    # I/O). As each finishes we print its buffered logs and persist its whole row
    # in ONE backend write, on the main thread — so the single-threaded sheet/CSV
    # backend is never touched concurrently, and a completed company is saved even
    # if a later one crashes.
    done = 0
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = {
            ex.submit(process_competitor, i, total, competitor, website, row,
                      col_map, args, client, icp_context, apify_limiter): (i, row)
            for (i, competitor, website, row) in work
        }
        for fut in as_completed(futures):
            i, row = futures[fut]
            row_num = i + 2
            try:
                pending, logs = fut.result()
            except Exception as e:  # noqa: BLE001 — one company can't kill the batch
                print(f"\n  ! Competitor at row {row_num} failed: {e}")
                continue
            print()
            for line in logs:
                print(line)
            if pending:
                backend.write_row(row_num, pending, row)
                print(f"  ✓ Wrote {len(pending)} field(s) in one update")
            done += 1

    print(f"\n{'='*60}")
    print(f"Done. Processed {done} competitors.")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
