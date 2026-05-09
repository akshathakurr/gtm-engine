"""One-off runner: small-talk scraper across the 'Email Cold' tab of Test Scrapers."""
import os
import sys
import json
import subprocess

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from scrapers.small_talk_scraper.scraper import scrape_small_talk

SHEET_ID = "16dYuL3ORXYESiYLHxi4aW-iLhQxHL7GyITmVha0gAjk"
TAB = "Email Cold"
SMALL_TALK_COL = "R"  # 18th column (index 17)


def gws_read_range(rng: str) -> list:
    out = subprocess.run(
        ["gws", "sheets", "spreadsheets", "values", "get",
         "--params", json.dumps({"spreadsheetId": SHEET_ID, "range": rng})],
        capture_output=True, text=True, check=True,
    )
    # gws prints a "Using keyring backend" line first; find JSON start
    text = out.stdout
    json_start = text.find("{")
    return json.loads(text[json_start:]).get("values", [])


def gws_write_cell(cell: str, value: str) -> None:
    body = {"values": [[value]]}
    subprocess.run(
        ["gws", "sheets", "spreadsheets", "values", "update",
         "--params", json.dumps({
             "spreadsheetId": SHEET_ID,
             "range": f"'{TAB}'!{cell}",
             "valueInputOption": "RAW",
         }),
         "--json", json.dumps(body)],
        capture_output=True, text=True, check=True,
    )


def main():
    rows = gws_read_range(f"'{TAB}'!A1:Z999")
    headers = rows[0]
    name_idx     = headers.index("Champion Name")
    title_idx    = headers.index("Champion Job Title")
    li_idx       = headers.index("Champion LinkedIn")
    company_idx  = headers.index("Company Name")
    talk_idx     = headers.index("Small Talk")
    print(f"name col {name_idx}, company col {company_idx}, linkedin col {li_idx}, small-talk col {talk_idx}")

    for i, row in enumerate(rows[1:], start=2):  # row 2 = first data row
        def cell(idx: int) -> str:
            return row[idx] if idx < len(row) else ""

        name = cell(name_idx).strip()
        company = cell(company_idx).strip()
        linkedin = cell(li_idx).strip()
        existing = cell(talk_idx).strip()

        print(f"\n=== Row {i}: {name} ({company}) ===")
        if not name:
            print("  no name, skipping"); continue
        if existing:
            print(f"  already filled, skipping: {existing[:80]}"); continue

        try:
            result = scrape_small_talk(
                profile_url=linkedin, name=name, company=company,
                max_signals=3,
            )
            small_talk = result.get("small_talk", "") or "(no humanizing signals found)"
        except Exception as e:
            small_talk = f"(error: {e})"
            print(f"  scraper error: {e}")

        print(f"  → {small_talk[:300]}")
        gws_write_cell(f"R{i}", small_talk)
        print(f"  written to R{i}")


if __name__ == "__main__":
    main()
