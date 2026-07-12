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
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

import anthropic

from config import CONTEXT_DIR
from workflows._common import (
    GoogleSheetsBackend, CsvBackend, find_col, ensure_col, cell, load_icp,
    RateLimiter, checkpoint_path, checkpoint_load, checkpoint_append,
    preview_and_confirm,
)
from workflows.competitor_analysis.steps import (
    OUTPUT_COLUMNS, COMPANY_CONCURRENCY, APIFY_MIN_INTERVAL,
    _run_parallel,
    scrape_website, find_official_site, find_linkedin_url, draft_description,
    get_firmographics, get_recent_news, find_founders, get_founder_post_type,
    extract_product_info, get_customer_reviews, get_deal_size, get_content_type,
    analyze_competitor,
)


# ---------------------------------------------------------------------------
# Per-SUB-STEP checkpoint — resume a half-finished competitor without re-paying
# ---------------------------------------------------------------------------
# The per-competitor checkpoint in main() only records a competitor once its
# WHOLE row is written, so a competitor that dies mid-enrichment (crash, Ctrl-C,
# credit-exhaustion) has NOTHING saved and re-pays every web search / Claude call
# / Apify scrape on resume. This finer checkpoint persists each column value the
# instant it's produced (keyed by competitor + column), so a resumed run reuses
# the completed lookups and only pays for what's still missing.

# Sentinel "column" for the resolved website URL (from find_official_site), so a
# no-URL competitor doesn't re-run that paid search on resume.
WEBSITE_KEY = "__website__"


class SubstepCheckpoint:
    """Thread-safe (competitor, column) → value cache backed by a JSONL file.

    Loaded once up front (before any worker threads start), then appended to
    concurrently as competitors run — each ``save`` is guarded by a lock and
    fsync'd, so a hard kill mid-write can't corrupt earlier entries.
    """

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._data = checkpoint_load(path)  # {"competitor\tcolumn": value}

    @staticmethod
    def _key(competitor: str, column: str) -> str:
        return f"{competitor}\t{column}"

    def prefilled(self, competitor: str) -> Dict[str, str]:
        """Already-completed {column: value} for one competitor (truthy only)."""
        prefix = f"{competitor}\t"
        return {
            k[len(prefix):]: v
            for k, v in self._data.items()
            if k.startswith(prefix) and v
        }

    def save(self, competitor: str, column: str, value: str) -> None:
        if not value:
            return
        with self._lock:
            checkpoint_append(self.path, self._key(competitor, column), value)


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
# Per-competitor orchestration (write-free — safe to run concurrently)
# ---------------------------------------------------------------------------

def process_competitor(
    index: int,
    total: int,
    competitor: str,
    website: str,
    row: List[str],
    col_map: Dict[str, int],
    args,
    client: anthropic.Anthropic,
    icp_context: str,
    apify_limiter,
    url_col=None,
    substep_ck: Optional["SubstepCheckpoint"] = None,
) -> tuple:
    """Run all 12 enrichment steps for one competitor and return the values to write.

    Does NO backend I/O: it buffers every output-column value into ``pending``
    (col_idx → value) and collects progress into ``logs``. The caller writes the
    row and prints the logs on the main thread, so companies can run concurrently
    without touching the single-threaded backend from multiple threads.

    ``apify_limiter`` (a shared _common.RateLimiter) spaces the throttled Apify
    actor calls — founder posts + reviews — across all concurrent companies.

    Returns ``(pending: Dict[int, str], logs: List[str])``.
    """
    logs: List[str] = []
    written: Dict[str, str] = {}
    pending: Dict[int, str] = {}

    def log(msg: str = "") -> None:
        logs.append(msg)

    def get_col(name: str) -> str:
        """Read from original row OR from this run's writes."""
        return written.get(name) or (
            row[col_map[name]].strip()
            if col_map.get(name) is not None and col_map[name] < len(row)
            else ""
        )

    def write_col(name: str, value: str) -> None:
        if value:
            pending[col_map[name]] = value
            written[name] = value
            if substep_ck is not None:
                substep_ck.save(competitor, name, value)

    # Resume: seed values this competitor already produced on a prior run. They
    # both (a) make get_col truthy so the paid lookup that produced them is
    # skipped, and (b) go into `pending` so they're re-written to the row — the
    # row was never persisted if the competitor died mid-run, so the cache is the
    # only copy. The resolved website (WEBSITE_KEY) is handled separately below.
    if substep_ck is not None:
        for name, value in substep_ck.prefilled(competitor).items():
            if name == WEBSITE_KEY:
                if not website:
                    website = value
            elif name in col_map:
                written[name] = value
                pending[col_map[name]] = value

    log(f"{'='*60}")
    log(f"[{index + 1}/{total}] {competitor}  ({website or 'no URL'})")
    log(f"{'='*60}")

    firm_cols = ["Employee Count", "Founded Year", "Last Funding Stage", "Total Funding", "Est. Revenue", "HQ Location"]
    f1_cols = ["Founder (1) Name", "Founder (1) LinkedIn", "Founder (1) Twitter"]
    f2_cols = ["Founder (2) Name", "Founder (2) LinkedIn", "Founder (2) Twitter"]
    product_cols = [
        "Target Persona (User)", "Sales Motion", "Primary CTA", "Pricing",
        "Customer Stories", "Product Features", "Top Logos", "Marketing Messaging", "SEO",
    ]

    # ── Step 1: Website scrape ────────────────────────────────────────────
    # Only scrape when something scrape-derived is still missing. On resume, if
    # those columns are already checkpointed, we skip the (paid Firecrawl) scrape
    # entirely — the same set gates the desc/product/content-type lookups below,
    # so skipping the scrape can't strand a lookup that needed it.
    scrape_dependent = ["Company Description", "Content Type"] + product_cols
    scraped: dict = {}
    if not website:
        # No URL on the row — find the official site first so website-derived
        # columns (description, product info, CTA…) still populate. Persisted to
        # the input URL column via pending (written on the main thread) and to the
        # sub-step checkpoint so a resumed run skips this paid search.
        log("  [1] No website URL — searching for the official site...")
        website = find_official_site(competitor, client)
        if website:
            if url_col is not None:
                pending[url_col] = website
            if substep_ck is not None:
                substep_ck.save(competitor, WEBSITE_KEY, website)
    if website and any(not get_col(c) for c in scrape_dependent):
        log(f"  [1] Scraping website ({website})...")
        scraped = scrape_website(website)
        log(f"    → {len(scraped.get('full_text_by_page', {}))} pages scraped")
    elif website:
        log("  [1] Scrape-derived columns already filled — skipping scrape")
    else:
        log("  [1] No website found — skipping scrape")

    # ── Steps 2–10: independent enrichment (Claude + Exa + reviews actor) ──
    firm_missing     = [c for c in firm_cols if not get_col(c)]
    missing_founders = [c for c in f1_cols + f2_cols if not get_col(c)]
    product_missing  = [c for c in product_cols if not get_col(c)] if scraped else []

    tasks: Dict[str, "callable"] = {}
    if not get_col("Company LinkedIn URL"):
        tasks["li_url"] = lambda: find_linkedin_url(competitor, website, client)
    if not get_col("Company Description") and scraped:
        tasks["desc"] = lambda: draft_description(competitor, scraped, client)
    if firm_missing:
        tasks["firm"] = lambda: get_firmographics(competitor, website, client)
    if not get_col("Recent News"):
        tasks["news"] = lambda: get_recent_news(competitor, website, client)
    if missing_founders:
        tasks["founders"] = lambda: find_founders(competitor, website, client)
    if product_missing:
        tasks["prod"] = lambda: extract_product_info(competitor, scraped, client)
    if not get_col("Deal Size"):
        tasks["deal"] = lambda: get_deal_size(competitor, website, client)
    if not args.skip_reviews and not get_col("Customer Reviews"):
        # Reviews hits a throttled Apify actor — gate it on the shared limiter.
        tasks["reviews"] = lambda: (apify_limiter.acquire() or get_customer_reviews(competitor, website, client))

    if tasks:
        log(f"  [2-10] Enriching — {len(tasks)} lookups in parallel: {', '.join(tasks)}")
        res = _run_parallel(tasks)
        for key, (_v, err) in res.items():
            if err:
                log(f"    ! {key} lookup failed: {err}")
    else:
        res = {}
        log("  [2-10] All enrichment columns already filled — skipping")

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

    # Step 9: Customer reviews. Only write the "not available" sentinel on a
    # *successful* empty lookup — an errored lookup leaves the cell blank so a
    # rerun retries it (a filled cell is skipped forever).
    if "reviews" in tasks:
        _rv, _rerr = res.get("reviews", (None, None))
        write_col("Customer Reviews", (_rv or "") or ("" if _rerr else "not available"))
    elif args.skip_reviews:
        log("  [9] Skipping reviews (--skip-reviews)")

    # Step 10: Deal size
    if "deal" in tasks:
        write_col("Deal Size", _val("deal", ""))

    # ── Step 7: Founder post types (throttled actors → gated + sequential) ─
    founder_post_summaries: List[str] = []
    if args.skip_founder_posts:
        log("  [7] Skipping founder posts (--skip-founder-posts)")
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
            log(f"  [7.{fi+1}] Getting post type for {fname}...")
            apify_limiter.acquire()  # space actor calls across all concurrent companies
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
            log(f"    → {pt[:100] or '(no data)'}")

    # ── Step 11: Content type ─────────────────────────────────────────────
    if not get_col("Content Type"):
        log("  [11] Synthesising content type...")
        ct = get_content_type(competitor, founder_post_summaries, scraped, client)
        write_col("Content Type", ct)
        log(f"    → {ct[:100] or '(none)'}")
    else:
        log("  [11] Content Type already filled — skipping")

    # ── Step 12: Final analysis ───────────────────────────────────────────
    if args.skip_analysis:
        log("  [12] Skipping analysis (--skip-analysis)")
    else:
        analysis_cols = ["Competitor Score", "Strength", "Weakness", "Target ICP"]
        missing_analysis = [c for c in analysis_cols if not get_col(c)]
        if missing_analysis:
            log("  [12] Running final analysis...")
            profile = {c: get_col(c) for c in OUTPUT_COLUMNS if get_col(c)}
            analysis = analyze_competitor(competitor, profile, icp_context, client)
            for c in missing_analysis:
                write_col(c, analysis.get(c, ""))
            log(f"    → Score: {analysis.get('Competitor Score','?')} | ICP: {analysis.get('Target ICP','?')}")
            log(f"    → Strength:  {analysis.get('Strength','')[:80]}")
            log(f"    → Weakness:  {analysis.get('Weakness','')[:80]}")
        else:
            log("  [12] Analysis columns already filled — skipping")

    return pending, logs


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

    # ---------------------------------------------------------------------------
    # CRASH-SAFETY: a checkpoint (local JSONL) records each competitor the instant
    # its row is written. If the run dies midway (crash, credit-exhaustion,
    # Ctrl-C), a re-run loads the checkpoint, skips every competitor already done,
    # and never re-pays for its searches / Claude calls / Apify actors.
    ck_id = args.sheet_id or (args.input_csv or "csv")
    ck_path = checkpoint_path(f"competitor_analysis_{ck_id}")
    # Finer per-sub-step checkpoint: a competitor interrupted mid-enrichment
    # (never reached the per-competitor checkpoint below) resumes from its
    # already-completed column lookups instead of re-paying every scrape.
    substep_ck = SubstepCheckpoint(checkpoint_path(f"competitor_analysis_substeps_{ck_id}"))
    done_competitors = set(checkpoint_load(ck_path).keys())
    if done_competitors:
        before = len(work)
        work = [w for w in work if w[1] not in done_competitors]
        skipped = before - len(work)
        if skipped:
            print(f"\n  Resuming from checkpoint: {skipped} competitor(s) already done — skipping.")
        if not work:
            print("  All competitors already completed. Nothing to do.")
            return

    # ---------------------------------------------------------------------------
    # SPEND GUARD: preview worst-case Apify spend before any actor runs. Worst
    # case per competitor = reviews (10) + founder LinkedIn posts (10) + founder
    # tweets (15), scaled by --skip-* flags. Gated on confirmation in interactive
    # mode; just printed under --auto.
    n = len(work)
    if not preview_and_confirm([
        ("Customer reviews (G2/Trustpilot)", "reviews",
         (0 if args.skip_reviews else n * 10)),
        ("Founder LinkedIn posts", "linkedin_profile_posts",
         (0 if args.skip_founder_posts else n * 10)),
        ("Founder tweets", "twitter",
         (0 if (args.skip_founder_posts or args.skip_twitter) else n * 15)),
    ], interactive=not args.auto):
        print("Aborted — no Apify runs were started.")
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
                      col_map, args, client, icp_context, apify_limiter, url_col,
                      substep_ck): (i, competitor, row)
            for (i, competitor, website, row) in work
        }
        for fut in as_completed(futures):
            i, competitor, row = futures[fut]
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
            # Checkpoint the instant the row is persisted, so a resumed run skips
            # this competitor entirely (per-company granularity is enough).
            row_dict = {name: pending[col_map[name]] for name in OUTPUT_COLUMNS
                        if col_map.get(name) in pending}
            checkpoint_append(ck_path, competitor, row_dict)
            done += 1

    print(f"\n{'='*60}")
    print(f"Done. Processed {done} competitors.")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
