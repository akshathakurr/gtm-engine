"""Run personalisation_hook skill on every lead in the Email Cold tab."""
import os
import sys
import json
import subprocess

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from skills.personalisation_hook import skill as hook_skill

SHEET_ID = "16dYuL3ORXYESiYLHxi4aW-iLhQxHL7GyITmVha0gAjk"
TAB = "Email Cold"
ICP_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "context", "icp.md"))


def gws_read(rng: str) -> list:
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


def col_letter(idx: int) -> str:
    s = ""
    n = idx
    while True:
        s = chr(ord("A") + n % 26) + s
        n = n // 26 - 1
        if n < 0:
            break
    return s


def main():
    icp_context = open(ICP_PATH).read() if os.path.exists(ICP_PATH) else ""

    rows = gws_read(f"'{TAB}'!A1:Z999")
    headers = rows[0]

    def idx(name): return headers.index(name)

    name_i      = idx("Champion Name")
    title_i     = idx("Champion Job Title")
    company_i   = idx("Company Name")
    desc_i      = idx("Company Description")
    emp_i       = idx("Employee")
    rev_i       = idx("Est. Revenue")
    fund_i      = idx("Total Funding")
    hq_i        = idx("HQ Location")
    smalltalk_i = idx("Small Talk")
    posts_i     = idx("LinkedIn Post")
    comp_i      = idx("Competitors")
    hook_i      = idx("Personalization Hook")

    hook_letter = col_letter(hook_i)

    for r, row in enumerate(rows[1:], start=2):
        def cell(i):
            return row[i].strip() if i < len(row) and row[i] else ""

        name = cell(name_i)
        if not name:
            continue

        company = cell(company_i)
        small_talk = cell(smalltalk_i)
        if small_talk == "(no humanizing signals found)":
            small_talk = ""

        post_urls = [u.strip() for u in cell(posts_i).split("\n") if u.strip()]
        matching_posts = [{"url": u, "text": "", "posted_at": ""} for u in post_urls]

        print(f"\n=== Row {r}: {name} ({company}) ===", flush=True)
        try:
            result = hook_skill.generate_hooks(
                name=name,
                company=company,
                position=cell(title_i),
                matching_posts=matching_posts,
                small_talk=small_talk,
                icp_context=icp_context,
                competitors=[c.strip() for c in cell(comp_i).split(",") if c.strip()],
                company_description=cell(desc_i),
                employee_count=cell(emp_i),
                est_revenue=cell(rev_i),
                total_funding=cell(fund_i),
                hq=cell(hq_i),
            )
            hooks = result.get("hooks", "")
            errs = result.get("errors", [])
        except Exception as e:
            hooks = ""
            errs = [f"exception: {e}"]
            print(f"  scraper error: {e}", flush=True)

        preview = hooks[:300] if hooks else f"(empty — {errs})"
        print(f"  → {preview}", flush=True)

        gws_write_cell(f"{hook_letter}{r}", hooks)
        print(f"  written to {hook_letter}{r}", flush=True)


if __name__ == "__main__":
    main()
