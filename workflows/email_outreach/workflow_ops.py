"""
Email Outreach — Data Operations CLI (interactive / Claude Code mode)

Subcommands:
  read-sheet       Read all rows from a Google Sheet
  web-search       Run a web search (enrichment, competitor lookup, buyer discovery)
  find-contact     Look up a person's email via Apollo
  find-company     Look up company info via Apollo
  write-column     Write a full column of values to a sheet
  write-cell       Write a single cell to a sheet

Usage:
  python3 workflow_ops.py read-sheet --sheet-id X
  python3 workflow_ops.py web-search --query "Acme Corp CEO founder"
  python3 workflow_ops.py find-contact --name "John Smith" --company "Acme Corp"
  python3 workflow_ops.py find-contact --linkedin-url "https://linkedin.com/in/jsmith"
  python3 workflow_ops.py find-company --domain "acme.com"
  python3 workflow_ops.py write-column --sheet-id X --col-name "Priority" --values '["P0","P1"]'
  python3 workflow_ops.py write-cell --sheet-id X --cell "E2" --value "Decision Maker"
"""

import json
import argparse
from typing import List

import subprocess

import config  # noqa: F401  side-effect: loads .env
from scrapers.web_search.scraper import search_web
from scrapers.contact_finder import scraper as _contact_scraper


# ---------------------------------------------------------------------------
# Inlined helpers
# ---------------------------------------------------------------------------

def gws_read_sheet(sheet_id: str, sheet_name: str) -> List[List[str]]:
    result = subprocess.run(
        ["gws", "sheets", "spreadsheets", "values", "get",
         "--params", json.dumps({"spreadsheetId": sheet_id, "range": sheet_name})],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout).get("values", [])


def gws_write_range(sheet_id: str, range_: str, values: List[List[str]]) -> None:
    subprocess.run(
        ["gws", "sheets", "spreadsheets", "values", "update",
         "--params", json.dumps({"spreadsheetId": sheet_id, "range": range_,
                                  "valueInputOption": "USER_ENTERED"}),
         "--json", json.dumps({"values": values})],
        capture_output=True, text=True, check=True,
    )


def col_letter(idx: int) -> str:
    result, n = "", idx + 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        result = chr(65 + rem) + result
    return result


def find_col(headers, *names):
    lower = [n.lower() for n in names]
    for i, h in enumerate(headers):
        if h.strip().lower() in lower:
            return i
    return None


def cmd_read_sheet(args):
    rows = gws_read_sheet(args.sheet_id, args.sheet_name)
    if not rows or len(rows) < 2:
        print(json.dumps({"error": "Sheet empty", "leads": []}))
        return
    headers   = rows[0]
    data_rows = rows[1:]

    def fc(*names):
        lower = [n.lower() for n in names]
        for i, h in enumerate(headers):
            if h.strip().lower() in lower:
                return i
        return None

    def c(row, idx):
        return row[idx].strip() if idx is not None and idx < len(row) else ""

    company_col  = fc("company", "company name")
    name_col     = fc("name", "full name", "person name")
    position_col = fc("position", "title", "job title", "role")
    linkedin_col = fc("linkedin", "linkedin profile", "linkedin url")
    email_col    = fc("email")

    leads = []
    for i, row in enumerate(data_rows):
        lead = {
            "row":      i + 2,
            "company":  c(row, company_col),
            "name":     c(row, name_col),
            "position": c(row, position_col),
            "linkedin": c(row, linkedin_col),
            "email":    c(row, email_col),
            "raw_row":  row,
        }
        if i == 0:
            lead["headers"] = headers
        leads.append(lead)

    print(json.dumps({"leads": leads, "total": len(leads)}, ensure_ascii=False, indent=2))


def cmd_web_search(args):
    try:
        result = search_web(
            query=args.query,
            num_results=args.num_results,
            summary_question=args.summary_question or None,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e), "query": args.query, "results": []}))


def cmd_find_contact(args):
    parts = (args.name or "").strip().split(" ", 1)
    first = parts[0] if parts else ""
    last  = parts[1] if len(parts) > 1 else ""
    try:
        result = _contact_scraper.find_contact(
            first_name=first or None,
            last_name=last or None,
            organization_name=args.company or None,
            linkedin_url=args.linkedin_url or None,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e), "found": False, "person": None}))


def cmd_find_company(args):
    try:
        result = _contact_scraper.find_company(domain=args.domain)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e), "found": False, "company": None}))


def cmd_write_column(args):
    rows = gws_read_sheet(args.sheet_id, args.sheet_name)
    if not rows:
        print(json.dumps({"error": "Sheet empty"}))
        return

    headers = list(rows[0])
    col_idx = None
    for i, h in enumerate(headers):
        if h.strip().lower() == args.col_name.strip().lower():
            col_idx = i
            break
    if col_idx is None:
        col_idx = len(headers)

    col_ltr = col_letter(col_idx)
    values  = json.loads(args.values)

    gws_write_range(args.sheet_id, f"{args.sheet_name}!{col_ltr}1", [[args.col_name]])
    gws_write_range(
        args.sheet_id,
        f"{args.sheet_name}!{col_ltr}2:{col_ltr}{len(values)+1}",
        [[v] for v in values],
    )
    print(json.dumps({"ok": True, "col": args.col_name, "col_letter": col_ltr, "rows_written": len(values)}))


def cmd_write_cell(args):
    gws_write_range(args.sheet_id, f"{args.sheet_name}!{args.cell}", [[args.value]])
    print(json.dumps({"ok": True, "cell": args.cell, "value": args.value}))


def main():
    parser = argparse.ArgumentParser(description="Email Outreach — Data Operations CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("read-sheet")
    p.add_argument("--sheet-id",   required=True)
    p.add_argument("--sheet-name", default="Sheet1")

    p = sub.add_parser("web-search")
    p.add_argument("--query",            required=True)
    p.add_argument("--num-results",      type=int, default=5)
    p.add_argument("--summary-question", default=None)

    p = sub.add_parser("find-contact")
    p.add_argument("--name",         default=None)
    p.add_argument("--company",      default=None)
    p.add_argument("--linkedin-url", default=None)

    p = sub.add_parser("find-company")
    p.add_argument("--domain", required=True)

    p = sub.add_parser("write-column")
    p.add_argument("--sheet-id",   required=True)
    p.add_argument("--sheet-name", default="Sheet1")
    p.add_argument("--col-name",   required=True)
    p.add_argument("--values",     required=True)

    p = sub.add_parser("write-cell")
    p.add_argument("--sheet-id",   required=True)
    p.add_argument("--sheet-name", default="Sheet1")
    p.add_argument("--cell",       required=True)
    p.add_argument("--value",      required=True)

    args = parser.parse_args()
    {
        "read-sheet":    cmd_read_sheet,
        "web-search":    cmd_web_search,
        "find-contact":  cmd_find_contact,
        "find-company":  cmd_find_company,
        "write-column":  cmd_write_column,
        "write-cell":    cmd_write_cell,
    }[args.command](args)


if __name__ == "__main__":
    main()
