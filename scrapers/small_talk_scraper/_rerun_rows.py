"""Rerun small-talk scraper on specific rows of the Email Cold sheet."""
import os
import sys
import json
import subprocess

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from scrapers.small_talk_scraper.scraper import scrape_small_talk

SHEET_ID = "16dYuL3ORXYESiYLHxi4aW-iLhQxHL7GyITmVha0gAjk"
TAB = "Email Cold"
ROWS_TO_RERUN = [5]  # Tyler — fact-level dedup retest


def gws_read_range(rng: str) -> list:
    out = subprocess.run(
        ["gws", "sheets", "spreadsheets", "values", "get",
         "--params", json.dumps({"spreadsheetId": SHEET_ID, "range": rng})],
        capture_output=True, text=True, check=True,
    )
    text = out.stdout
    return json.loads(text[text.find("{"):]).get("values", [])


def gws_write_cell(cell: str, value: str) -> None:
    subprocess.run(
        ["gws", "sheets", "spreadsheets", "values", "update",
         "--params", json.dumps({
             "spreadsheetId": SHEET_ID,
             "range": f"'{TAB}'!{cell}",
             "valueInputOption": "RAW",
         }),
         "--json", json.dumps({"values": [[value]]})],
        capture_output=True, text=True, check=True,
    )


def main():
    rows = gws_read_range(f"'{TAB}'!A1:Z999")
    headers = rows[0]
    name_idx    = headers.index("Champion Name")
    li_idx      = headers.index("Champion LinkedIn")
    company_idx = headers.index("Company Name")

    for row_num in ROWS_TO_RERUN:
        row = rows[row_num - 1]

        def cell(idx: int) -> str:
            return row[idx] if idx < len(row) else ""

        name = cell(name_idx).strip()
        company = cell(company_idx).strip()
        linkedin = cell(li_idx).strip()

        print(f"\n=== Row {row_num}: {name} ({company}) | LinkedIn: {linkedin} ===", flush=True)
        try:
            result = scrape_small_talk(
                profile_url=linkedin, name=name, company=company,
                max_signals=3,
            )
            small_talk = result.get("small_talk", "") or "(no humanizing signals found)"
        except Exception as e:
            small_talk = f"(error: {e})"
            print(f"  scraper error: {e}", flush=True)

        print(f"  → {small_talk[:400]}", flush=True)
        gws_write_cell(f"R{row_num}", small_talk)
        print(f"  written to R{row_num}", flush=True)


if __name__ == "__main__":
    main()
