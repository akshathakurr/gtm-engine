"""Shared helpers for GTM workflows.

Every workflow reads/writes Google Sheets through the ``gws`` CLI, parses the
same ``context.md`` questionnaire, and coerces Claude's JSON replies. Those
helpers used to be copy-pasted into each workflow, which let them drift apart
(most visibly ``strip_json_fence`` — some copies could not survive a model that
reasons in prose before emitting JSON). They now live here once.
"""

import os
import re
import json
import subprocess
from typing import List, Optional

from config import CONTEXT_DIR

# Filename inside CONTEXT_DIR where workflows read/write user answers.
CONTEXT_FILE = "context.md"


# ---------------------------------------------------------------------------
# Claude JSON replies
# ---------------------------------------------------------------------------

def strip_json_fence(text: str) -> str:
    """Return the JSON payload from a Claude reply.

    Strips a ```` ``` ```` code fence if present, then — because the model
    sometimes reasons in prose before emitting the JSON — carves out the
    outermost ``{...}`` object or ``[...]`` array.
    """
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1]).strip()
    if text and text[0] not in "{[":
        starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
        ends = [i for i in (text.rfind("}"), text.rfind("]")) if i != -1]
        if starts and ends and max(ends) > min(starts):
            text = text[min(starts):max(ends) + 1].strip()
    return text


# ---------------------------------------------------------------------------
# Google Sheets (via the `gws` CLI)
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
    """Convert a 0-based column index to an A1 column letter (0 -> 'A')."""
    result, n = "", idx + 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        result = chr(65 + rem) + result
    return result


def find_col(headers: List[str], *names: str) -> Optional[int]:
    """Return the index of the first header matching any of ``names`` (case-insensitive)."""
    lower = [n.lower() for n in names]
    for i, h in enumerate(headers):
        if h.strip().lower() in lower:
            return i
    return None


def ensure_col(headers: List[str], name: str) -> int:
    """Return the index of ``name`` in ``headers``, appending it if absent."""
    idx = find_col(headers, name)
    if idx is not None:
        return idx
    headers.append(name)
    return len(headers) - 1


def cell(row: List[str], idx: Optional[int]) -> str:
    """Return the stripped value at ``idx``, or '' if out of range / ``idx`` is None."""
    return row[idx].strip() if idx is not None and idx < len(row) else ""


# ---------------------------------------------------------------------------
# context.md — the business questionnaire
# ---------------------------------------------------------------------------

def strip_scaffolding(text: str) -> str:
    """For each ``## Section`` that has an ``### Answer``, keep only the answer
    body; otherwise keep the section as-is (legacy context files)."""
    out = []
    for m in re.finditer(r"(?ms)^(##\s+[^\n]+)\n(.*?)(?=^##\s+|\Z)", text):
        header, body = m.group(1), m.group(2)
        ans = re.search(r"(?ms)^###\s+Answer\s*$\n(.*?)(?=^###\s+|\Z)", body)
        if ans:
            kept = "\n".join(ln for ln in ans.group(1).splitlines()
                             if not ln.strip().startswith("<!--")).strip()
            if kept and kept.lower() not in ("(fill this in)", "(none)", "(skip)"):
                out.append(f"{header}\n{kept}")
        else:
            stripped = body.strip()
            if stripped:
                out.append(f"{header}\n{stripped}")
    return "\n\n".join(out) if out else text


def load_icp() -> str:
    """Concatenate every ``context/*.md`` file (excluding ``.example`` templates),
    with questionnaire scaffolding stripped, into one context blob."""
    parts = []
    for fname in sorted(os.listdir(CONTEXT_DIR)):
        if fname.endswith(".md") and ".example" not in fname:
            with open(os.path.join(CONTEXT_DIR, fname)) as f:
                parts.append(strip_scaffolding(f.read()))
    if not parts:
        raise FileNotFoundError(
            f"No context .md files found in {CONTEXT_DIR}. "
            f"Copy context/context.md.example to context/context.md and fill it in."
        )
    return "\n\n---\n\n".join(parts)


def read_context_file() -> str:
    """Return the raw text of ``context/context.md`` ('' if it doesn't exist)."""
    path = os.path.join(CONTEXT_DIR, CONTEXT_FILE)
    if not os.path.exists(path):
        return ""
    with open(path) as f:
        return f.read()


def append_to_context_file(section_header: str, body: str) -> None:
    """Append a ``## section_header`` block to ``context/context.md`` (creating it if needed)."""
    path = os.path.join(CONTEXT_DIR, CONTEXT_FILE)
    body = body.strip()
    if not body:
        return
    block = f"\n\n## {section_header}\n{body}\n"
    if os.path.exists(path):
        with open(path, "a") as f:
            f.write(block)
    else:
        with open(path, "w") as f:
            f.write("# Context\n" + block)


# ---------------------------------------------------------------------------
# Apify spend preview
# ---------------------------------------------------------------------------

# USD per returned item, by source key. Keep in sync with each scraper's README.
APIFY_UNIT_COST = {
    "linkedin_post_search":   0.005,    # apimaestro posts-search   ($5/1k)
    "linkedin_profile_posts": 0.005,    # apimaestro profile-posts  ($5/1k)
    "linkedin_comments":      0.0012,   # apimaestro comments       ($1.2/1k)
    "linkedin_profile":       0.003,    # apimaestro profile        ($3/1k)
    "twitter":                0.00025,  # kaitoeasyapi tweets  (~$0.25/1k, upper bound)
}


def estimate_apify_cost(items):
    """Estimate worst-case Apify spend.

    ``items`` is an iterable of ``(label, source_key, count)`` tuples where
    ``source_key`` indexes ``APIFY_UNIT_COST``. Returns ``(rows, total_usd)``
    with ``rows = [(label, count, unit_cost, subtotal)]``. Unknown source keys
    cost 0 (they don't hit a paid actor).
    """
    rows, total = [], 0.0
    for label, source, count in items:
        unit = APIFY_UNIT_COST.get(source, 0.0)
        subtotal = unit * count
        rows.append((label, count, unit, subtotal))
        total += subtotal
    return rows, total


def preview_and_confirm(items, interactive=True, header="Estimated Apify spend"):
    """Print an itemized worst-case Apify cost estimate before a run.

    Costs are worst-case: they assume every requested item is returned and
    billed. Actual spend is usually lower (fewer results available, dedupe,
    date cutoffs). In interactive mode the user is asked to confirm — returns
    True to proceed, False to abort. In non-interactive mode the estimate is
    printed for the record and this always returns True.
    """
    rows, total = estimate_apify_cost([i for i in items if i[2] > 0])
    if not rows:
        return True
    print(f"\n  {header} (worst case — actual is usually lower):")
    for label, count, unit, subtotal in rows:
        print(f"    {label:<32} {count:>5} items x ${unit:.4f} = ${subtotal:7.2f}")
    print(f"    {'TOTAL':<32} {'':>5}                  ${total:7.2f}")
    if not interactive:
        return True
    try:
        resp = input("  Proceed with this run? [y/N] ").strip().lower()
    except EOFError:
        return False
    return resp in ("y", "yes")


def section_body(text: str, header: str) -> str:
    """Return the body under a ``## Header`` section, stripping placeholders."""
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
    return body
