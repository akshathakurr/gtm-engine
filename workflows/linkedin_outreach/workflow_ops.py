"""
LinkedIn Outreach — Data Operations CLI

Used by Claude Code in interactive mode. Handles all I/O (sheet reads/writes,
web search, post scraping) so Claude can do the reasoning without an ANTHROPIC_API_KEY.

In interactive mode, Claude Code calls these subcommands and reasons over the output itself.
In autonomous mode, workflow.py handles everything end-to-end.

Subcommands:
  read-sheet       Read all leads from a Google Sheet tab
  web-search       Run a web search query (used for enrichment + competitor lookup)
  scrape-posts     Scrape LinkedIn posts for a profile URL
  write-column     Write a full column of values to a sheet (one value per data row)
  write-cell       Write a single cell to a sheet

Usage examples:
  python3 workflow_ops.py read-sheet --sheet-id X
  python3 workflow_ops.py read-sheet --sheet-id X --sheet-name "April Leads"
  python3 workflow_ops.py web-search --query "Acme Corp employees revenue funding headquarters"
  python3 workflow_ops.py web-search --query "Acme Corp direct competitors" --num-results 5
  python3 workflow_ops.py scrape-posts --profile-url "https://www.linkedin.com/in/username/"
  python3 workflow_ops.py scrape-posts --profile-url "https://www.linkedin.com/in/username/" --max-posts 20 --days-back 60
  python3 workflow_ops.py write-column --sheet-id X --col-name "Priority" --values '["P0","P1","P2"]'
  python3 workflow_ops.py write-column --sheet-id X --sheet-name "Leads" --col-name "Reasoning" --values '["reason1","reason2"]'
  python3 workflow_ops.py write-cell --sheet-id X --cell "E2" --value "Decision Maker"
"""

import json
import argparse

import config  # noqa: F401  side-effect: loads .env
from workflows._common import (
    gws_read_sheet as _gws_read, gws_write_range as _gws_write,
    col_letter as _col_letter, find_col, cell,
)
from scrapers.web_search.scraper import search_web
from scrapers.linkedin_profile_post_scraper import scraper as _post_scraper


# ---------------------------------------------------------------------------
# Subcommand: read-sheet
# ---------------------------------------------------------------------------

def cmd_read_sheet(args: argparse.Namespace) -> None:
    """
    Read all leads from a Google Sheet and print as a JSON array.

    Output schema:
    [
      {
        "row": 2,                          // 1-based sheet row number
        "name": "...",
        "company": "...",
        "linkedin": "...",
        "position": "...",
        "headers": ["Name", "Company", ...],  // only on first item
        "raw_row": ["val1", "val2", ...]      // full raw row for reference
      },
      ...
    ]
    """
    rows = _gws_read(args.sheet_id, args.sheet_name)
    if not rows or len(rows) < 2:
        print(json.dumps({"error": "Sheet empty or no data rows", "leads": []}))
        return

    headers = rows[0]
    data_rows = rows[1:]

    name_col     = find_col(headers, "name", "full name")
    company_col  = find_col(headers, "company", "company name")
    linkedin_col = find_col(headers, "linkedin", "linkedin profile", "linkedin url")
    position_col = find_col(headers, "position", "title", "job title", "role")

    leads = []
    for i, row in enumerate(data_rows):
        lead = {
            "row": i + 2,
            "name":     cell(row, name_col),
            "company":  cell(row, company_col),
            "linkedin": cell(row, linkedin_col),
            "position": cell(row, position_col),
            "raw_row":  row,
        }
        if i == 0:
            lead["headers"] = headers
        leads.append(lead)

    print(json.dumps({"leads": leads, "total": len(leads)}, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# Subcommand: web-search
# ---------------------------------------------------------------------------

def cmd_web_search(args: argparse.Namespace) -> None:
    """
    Run a web search and print raw results as JSON.
    Claude Code interprets the results (for enrichment, competitor lookup, etc.).

    Output: the raw dict from the Web Search scraper (query, total, results, errors).
    """
    try:
        result = search_web(
            query=args.query,
            num_results=args.num_results,
            summary_question=args.summary_question or None,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e), "query": args.query, "results": []}))


# ---------------------------------------------------------------------------
# Subcommand: scrape-posts
# ---------------------------------------------------------------------------

def cmd_scrape_posts(args: argparse.Namespace) -> None:
    """
    Scrape LinkedIn posts for a profile URL and print as JSON.
    Claude Code filters and selects the relevant ones.

    Output: the processed posts list from the LinkedIn Profile Post Scraper.
    """
    try:
        result = _post_scraper.scrape_linkedin_profile_posts(
            profile_url=args.profile_url,
            max_posts=args.max_posts,
            days_back=args.days_back if args.days_back is not None else 90,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e), "profile_url": args.profile_url, "posts": []}))


# ---------------------------------------------------------------------------
# Subcommand: write-column
# ---------------------------------------------------------------------------

def cmd_write_column(args: argparse.Namespace) -> None:
    """
    Write a full column to a sheet.

    --values is a JSON array of strings, one per data row (starting at row 2).
    The column is located by name; if it doesn't exist it's appended.
    """
    rows = _gws_read(args.sheet_id, args.sheet_name)
    if not rows:
        print(json.dumps({"error": "Sheet empty"}))
        return

    headers = list(rows[0])

    # Find or append column
    col_idx = find_col(headers, args.col_name)
    if col_idx is None:
        col_idx = len(headers)
        headers.append(args.col_name)

    col_ltr = _col_letter(col_idx)
    values  = json.loads(args.values)

    # Write header
    _gws_write(args.sheet_id, f"{args.sheet_name}!{col_ltr}1", [[args.col_name]])

    # Write data rows
    _gws_write(
        args.sheet_id,
        f"{args.sheet_name}!{col_ltr}2:{col_ltr}{len(values) + 1}",
        [[v] for v in values],
    )

    print(json.dumps({
        "ok": True,
        "col": args.col_name,
        "col_letter": col_ltr,
        "rows_written": len(values),
    }))


# ---------------------------------------------------------------------------
# Subcommand: write-cell
# ---------------------------------------------------------------------------

def cmd_write_cell(args: argparse.Namespace) -> None:
    """
    Write a single cell to a sheet.
    --cell accepts A1 notation (e.g. "E2") or "ColName:RowNum" (e.g. "Priority:3").
    """
    cell_ref = args.cell
    if ":" in cell_ref and not cell_ref[0].isalpha():
        # "ColName:RowNum" format — resolve to A1
        col_name, row_num = cell_ref.split(":", 1)
        rows = _gws_read(args.sheet_id, args.sheet_name)
        headers = rows[0] if rows else []
        col_idx = find_col(headers, col_name)
        if col_idx is None:
            print(json.dumps({"error": f"Column '{col_name}' not found"}))
            return
        cell_ref = f"{_col_letter(col_idx)}{row_num}"

    _gws_write(args.sheet_id, f"{args.sheet_name}!{cell_ref}", [[args.value]])
    print(json.dumps({"ok": True, "cell": cell_ref, "value": args.value}))


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="LinkedIn Outreach — Data Operations CLI (for Claude Code interactive mode)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # read-sheet
    p_read = sub.add_parser("read-sheet", help="Read leads from a Google Sheet")
    p_read.add_argument("--sheet-id",   required=True)
    p_read.add_argument("--sheet-name", default="Sheet1")

    # web-search
    p_search = sub.add_parser("web-search", help="Run a web search query")
    p_search.add_argument("--query",            required=True)
    p_search.add_argument("--num-results",      type=int, default=5)
    p_search.add_argument("--summary-question", default=None)

    # scrape-posts
    p_posts = sub.add_parser("scrape-posts", help="Scrape LinkedIn posts for a profile")
    p_posts.add_argument("--profile-url", required=True)
    p_posts.add_argument("--max-posts",   type=int, default=15)
    p_posts.add_argument("--days-back",   type=int, default=None)

    # write-column
    p_wcol = sub.add_parser("write-column", help="Write a full column to a sheet")
    p_wcol.add_argument("--sheet-id",   required=True)
    p_wcol.add_argument("--sheet-name", default="Sheet1")
    p_wcol.add_argument("--col-name",   required=True, help="Column header name")
    p_wcol.add_argument("--values",     required=True, help="JSON array of values (one per data row)")

    # write-cell
    p_wcell = sub.add_parser("write-cell", help="Write a single cell to a sheet")
    p_wcell.add_argument("--sheet-id",   required=True)
    p_wcell.add_argument("--sheet-name", default="Sheet1")
    p_wcell.add_argument("--cell",       required=True, help="A1 notation e.g. E2")
    p_wcell.add_argument("--value",      required=True)

    args = parser.parse_args()

    dispatch = {
        "read-sheet":    cmd_read_sheet,
        "web-search":    cmd_web_search,
        "scrape-posts":  cmd_scrape_posts,
        "write-column":  cmd_write_column,
        "write-cell":    cmd_write_cell,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
