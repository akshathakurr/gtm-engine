"""Shared helpers for GTM workflows.

Every workflow reads/writes Google Sheets through the ``gws`` CLI, parses the
same ``context.md`` questionnaire, and coerces Claude's JSON replies. Those
helpers used to be copy-pasted into each workflow, which let them drift apart
(most visibly ``strip_json_fence`` — some copies could not survive a model that
reasons in prose before emitting JSON). They now live here once.
"""

import os
import re
import csv
import json
import math
import shutil
import subprocess
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Sequence

import anthropic

from config import CONTEXT_DIR, CLAUDE_MODEL

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

# Sheets API throttles per-minute writes; concurrent workflows sharing one
# account hit transient 429/5xx errors that used to kill a whole run on a
# single cell. Retry with backoff; permanent errors (bad range, grid limits,
# auth) are re-raised immediately.
_GWS_NON_RETRIABLE = ("exceeds grid limits", "Unable to parse range", "PERMISSION_DENIED",
                      "invalid_grant", "not found")


def _gws_run_with_retry(cmd: List[str], attempts: int = 4) -> "subprocess.CompletedProcess":
    for attempt in range(1, attempts + 1):
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            return result
        err = (result.stderr or "") + (result.stdout or "")
        if attempt == attempts or any(s.lower() in err.lower() for s in _GWS_NON_RETRIABLE):
            raise subprocess.CalledProcessError(result.returncode, cmd,
                                                output=result.stdout, stderr=result.stderr)
        time.sleep(2 ** attempt)  # 2s, 4s, 8s
    raise RuntimeError("unreachable")


def gws_read_sheet(sheet_id: str, sheet_name: str) -> List[List[str]]:
    result = _gws_run_with_retry(
        ["gws", "sheets", "spreadsheets", "values", "get",
         "--params", json.dumps({"spreadsheetId": sheet_id, "range": sheet_name})],
    )
    return json.loads(result.stdout).get("values", [])


def gws_write_range(sheet_id: str, range_: str, values: List[List[str]]) -> None:
    _gws_run_with_retry(
        ["gws", "sheets", "spreadsheets", "values", "update",
         "--params", json.dumps({"spreadsheetId": sheet_id, "range": range_,
                                 "valueInputOption": "USER_ENTERED"}),
         "--json", json.dumps({"values": values})],
    )


def _parse_gws_json(stdout: str):
    """Parse the JSON body from gws stdout, tolerating a preamble line.

    gws may print a banner (e.g. "Using keyring backend: ...") before the JSON.
    ``strip_json_fence`` can mis-carve if that preamble contains a ``{``/``[``,
    so instead we find the first line that opens a JSON value and parse from
    there. Returns the parsed value, or ``None`` if nothing parses.
    """
    lines = (stdout or "").splitlines()
    for i, ln in enumerate(lines):
        if ln.lstrip()[:1] in ("{", "["):
            try:
                return json.loads("\n".join(lines[i:]))
            except ValueError:
                continue
    return None


# NOTE: gws_available / gws_create_sheet / rows_to_values / write_input_csv have
# no in-repo Python callers — they are Claude Code's session-time API (see
# CLAUDE.md "Where the output goes" / "When the ask is vague"). Don't delete.
def gws_available(timeout: int = 15) -> bool:
    """Return True if the ``gws`` CLI is installed *and* usably authenticated.

    Used to decide output routing: a usable ``gws`` means we can create/write a
    Google Sheet; otherwise the caller falls back to a CSV. This is deliberately
    conservative — it checks ``gws auth status`` (no API call, no scope needed)
    rather than a live request, so it never has side effects.

    Note ``gws`` exits 0 even on errors (it signals failure via an ``error`` key
    or ``token_error`` in its JSON), so we inspect the payload, not the exit code.
    A merely-expired access token is fine (a valid refresh token mints a new one);
    a *revoked* refresh token is not — that needs a fresh ``gws auth login``.
    """
    if shutil.which("gws") is None:
        return False
    try:
        result = subprocess.run(
            ["gws", "auth", "status"],
            capture_output=True, text=True, timeout=timeout,
        )
        data = _parse_gws_json(result.stdout)
    except (subprocess.SubprocessError, OSError):
        return False
    if not isinstance(data, dict) or "error" in data:
        return False
    if data.get("token_valid"):
        return True
    # Access token stale but refreshable — usable unless the grant was revoked.
    if data.get("has_refresh_token"):
        return "revok" not in (data.get("token_error") or "").lower()
    return False


def gws_create_sheet(title: str, sheet_name: str = "Sheet1") -> str:
    """Create a new Google Spreadsheet and return its spreadsheet ID.

    The workflows all accept a ``--sheet-id``; this is the "make one for them"
    path when the user has no sheet but ``gws`` is available. Populate it after
    creation with :func:`gws_write_range`.
    """
    result = subprocess.run(
        ["gws", "sheets", "spreadsheets", "create",
         "--json", json.dumps({
             "properties": {"title": title},
             "sheets": [{"properties": {"title": sheet_name}}],
         })],
        capture_output=True, text=True, timeout=30,
    )
    data = _parse_gws_json(result.stdout)
    if not isinstance(data, dict):
        raise RuntimeError(f"gws returned no parseable response creating '{title}'")
    if "error" in data or "spreadsheetId" not in data:
        msg = data.get("error", {}).get("message") if isinstance(data.get("error"), dict) else data.get("error")
        raise RuntimeError(f"Could not create Google Sheet '{title}': {msg or 'unknown error'}")
    return data["spreadsheetId"]


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
# Input files — turn a pasted list into a workflow's expected input
# ---------------------------------------------------------------------------
#
# When a user hands over a raw list ("here are 6 companies") with no sheet or
# CSV, we build the input file for them in the structure the target workflow
# reads. ``rows`` is a list of dicts keyed by ``fieldnames``; missing keys write
# blank. Route to CSV (no gws) or Sheet (gws available) with the two helpers.

def rows_to_values(fieldnames: Sequence[str], rows: List[Dict[str, str]]) -> List[List[str]]:
    """Header row + data rows as list-of-lists, ready for :func:`gws_write_range`."""
    values = [list(fieldnames)]
    for row in rows:
        values.append([str(row.get(f, "") or "") for f in fieldnames])
    return values


def write_input_csv(path: str, fieldnames: Sequence[str], rows: List[Dict[str, str]]) -> str:
    """Write ``rows`` to ``path`` as a CSV with ``fieldnames`` as the header.

    Returns ``path``. Extra keys not in ``fieldnames`` are ignored; missing
    keys write blank — so the file always matches the workflow's expected shape.
    """
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(fieldnames), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({f: row.get(f, "") for f in fieldnames})
    return path


# ---------------------------------------------------------------------------
# TabularStore — one row store, backed by a Google Sheet OR a local CSV
# ---------------------------------------------------------------------------
#
# The three "generated from scratch" workflows (content_idea_finder,
# linkedin_comment_helper, blog_builder) used to be Sheets-only. This gives them
# a CSV fallback for when the user won't connect Google, without each workflow
# re-implementing read/append/update twice. Construct with exactly one target:
# a ``sheet_id`` (+ ``sheet_name``) or a ``csv_path``.

class TabularStore:
    def __init__(self, sheet_id: Optional[str] = None, sheet_name: str = "Sheet1",
                 csv_path: Optional[str] = None):
        if bool(sheet_id) == bool(csv_path):
            raise ValueError("TabularStore needs exactly one of sheet_id or csv_path")
        self.sheet_id = sheet_id
        self.sheet_name = sheet_name
        self.csv_path = csv_path

    @property
    def is_csv(self) -> bool:
        return self.csv_path is not None

    def label(self) -> str:
        return f"CSV '{self.csv_path}'" if self.is_csv else f"sheet tab '{self.sheet_name}'"

    def read_all(self) -> List[List[str]]:
        """Return every row as a list of lists (empty list if nothing yet)."""
        if self.is_csv:
            if not os.path.exists(self.csv_path):
                return []
            with open(self.csv_path, newline="") as f:
                return [row for row in csv.reader(f)]
        return gws_read_sheet(self.sheet_id, self.sheet_name)

    @staticmethod
    def _raise_on_gws_error(result, action: str) -> None:
        """Surface a gws failure as a clean message. gws exits 0 even on errors
        (signalling via an ``error`` key), so check the payload too, not just rc.
        """
        out = (result.stdout or "").strip()
        data = _parse_gws_json(out)
        err_obj = data.get("error") if isinstance(data, dict) else None
        if result.returncode != 0 or err_obj is not None:
            msg = err_obj.get("message") if isinstance(err_obj, dict) else err_obj
            detail = msg or (result.stderr or "").strip() or out[:300] or "unknown error"
            raise RuntimeError(f"Google Sheets {action} failed: {detail}")

    def append(self, rows: List[List[str]]) -> None:
        """Append ``rows`` to the end. Caller is responsible for headers."""
        if not rows:
            return
        if self.is_csv:
            with open(self.csv_path, "a", newline="") as f:
                csv.writer(f).writerows([[str(c) for c in r] for r in rows])
            return
        # Anchor the range at A1: the append API "detects a table" around the
        # given range and can anchor at a non-A column on sheets with stray
        # cells; pinning A1 makes appends land at column A deterministically.
        cmd = ["gws", "sheets", "spreadsheets", "values", "append",
               "--params", json.dumps({
                   "spreadsheetId": self.sheet_id, "range": f"{self.sheet_name}!A1",
                   "valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS",
               }),
               "--json", json.dumps({"values": [[str(c) for c in r] for r in rows]})]
        # Retry only clearly-rejected transient failures (429/5xx). A rejected
        # request never landed, so retrying can't duplicate rows; anything
        # ambiguous or permanent raises immediately.
        for attempt in (1, 2, 3):
            result = subprocess.run(cmd, capture_output=True, text=True)
            try:
                self._raise_on_gws_error(result, "append")
                return
            except RuntimeError as e:
                transient = any(s in str(e).lower() for s in
                                ("429", "rate limit", "ratelimit", "quota exceeded",
                                 "unavailable", "internal error", "backend error"))
                if attempt == 3 or not transient:
                    raise
                time.sleep(2 ** attempt)  # 2s, 4s

    def update_row(self, row_idx: int, values: List[str]) -> None:
        """Overwrite the 1-indexed row ``row_idx`` with ``values``."""
        vals = [str(c) for c in values]
        if self.is_csv:
            rows = self.read_all()
            while len(rows) < row_idx:
                rows.append([])
            rows[row_idx - 1] = vals
            # Atomic rewrite: write a temp file then replace, so a crash mid-write
            # can't truncate the existing CSV.
            tmp = f"{self.csv_path}.tmp"
            with open(tmp, "w", newline="") as f:
                csv.writer(f).writerows(rows)
            os.replace(tmp, self.csv_path)
            return
        rng = f"{self.sheet_name}!A{row_idx}:{col_letter(len(vals) - 1)}{row_idx}"
        gws_write_range(self.sheet_id, rng, [vals])

    def ensure_headers(self, headers: List[str]) -> None:
        """Write ``headers`` as row 1 if the store is empty."""
        if not self.read_all():
            self.append([headers])

    def append_mapped(self, expected_headers: List[str],
                      rows: List[Dict[str, str]]) -> None:
        """Append ``rows`` (dicts keyed by header name) aligned to the store's
        ACTUAL header row, so values land under the right columns even when an
        existing sheet/CSV's column order differs from ``expected_headers``.

        Writes ``expected_headers`` as the header row if the store is empty. If
        the store already has a header row that's missing any expected column,
        warn (rather than silently writing values into the wrong columns, which
        is what positional appends do)."""
        existing = self.read_all()
        if not existing:
            self.append([expected_headers])
            actual = list(expected_headers)
        else:
            actual = existing[0]
            missing = [h for h in expected_headers if find_col(actual, h) is None]
            if missing:
                print(f"  ⚠ output {self.label()} is missing column(s) "
                      f"{missing}; those values will be dropped. Point at an "
                      f"empty tab/file to get the full schema.")
        values: List[List[str]] = []
        for row in rows:
            line = [""] * len(actual)
            for key, val in row.items():
                idx = find_col(actual, key)
                if idx is not None:
                    line[idx] = val
            values.append(line)
        self.append(values)


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
    "linkedin_post_search":   0.002,    # harvestapi post-search    ($2/1k)
    "linkedin_profile_posts": 0.002,    # harvestapi profile-posts  ($2/1k)
    "linkedin_comments":      0.0012,   # apimaestro comments       ($1.2/1k)
    "linkedin_profile":       0.004,    # harvestapi profile detail ($4/1k)
    "reviews":                0.003,    # zen-studio g2 reviews     ($3/1k)
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


# ---------------------------------------------------------------------------
# Bounded concurrency with call-start rate limiting
# ---------------------------------------------------------------------------
#
# The outreach workflows fan a per-lead LLM/scraper step across a small pool of
# threads. Compute runs concurrently; the (single-threaded) sheet/CSV backend is
# written sequentially afterwards, so it is never touched from two threads.

DEFAULT_CONCURRENCY = 6


class RateLimiter:
    """Spaces successive acquire() calls >= min_interval apart (thread-safe).

    Spacing applies to call *starts*, so slow work after acquire() overlaps
    across threads without bursting a rate-limited service.
    """

    def __init__(self, min_interval: float):
        self._min_interval = min_interval
        self._lock = threading.Lock()
        self._next = 0.0

    def acquire(self) -> None:
        if self._min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._next - now
            if wait > 0:
                time.sleep(wait)
                now += wait
            self._next = now + self._min_interval


def map_rate_limited(fn, items: list, *, min_interval: float = 0.0,
                     max_workers: int = DEFAULT_CONCURRENCY, on_result=None):
    """Run fn(item) over a bounded pool, starts spaced >= min_interval.

    Returns (results, errors) aligned to ``items``. fn exceptions are captured
    (result None, error set), never raised, so one bad item can't kill the batch.
    Results preserve input order.

    ``on_result(idx, item, result, error)``, if given, is called ON THE MAIN
    THREAD as each item finishes (in completion order) — use it to persist
    partial progress incrementally, so a mid-run crash or credit-exhaustion
    leaves everything completed-so-far already saved (never re-pay for it).
    """
    n = len(items)
    if n == 0:
        return [], []
    limiter = RateLimiter(min_interval)
    results: List[Optional[object]] = [None] * n
    errors: List[Optional[Exception]] = [None] * n

    def task(idx, item):
        limiter.acquire()
        try:
            return idx, fn(item), None
        except Exception as e:  # noqa: BLE001 — captured per item, surfaced to caller
            return idx, None, e

    with ThreadPoolExecutor(max_workers=min(max_workers, n)) as ex:
        futs = [ex.submit(task, i, it) for i, it in enumerate(items)]
        for fut in as_completed(futs):
            idx, res, err = fut.result()
            results[idx] = res
            errors[idx] = err
            if on_result is not None:
                # Runs on the main thread — safe to touch the sheet backend here.
                on_result(idx, items[idx], res, err)
    return results, errors


def collect_chunk_results(chunks, results, errors, blank, *, label):
    """Flatten per-chunk LLM results back to one per-row list, in order.

    The chunked score/classify steps all share this tail: a failed chunk comes
    back as ``None`` and must be padded with ``blank(chunk)`` so later chunks stay
    row-aligned — but a silent pad is indistinguishable from genuinely-empty data,
    so log which chunk failed and why. Centralised here so all channels report
    failures the same way instead of each re-inlining (and drifting from) it.
    """
    out = []
    for i, (chunk, res, err) in enumerate(zip(chunks, results, errors)):
        if res is not None:
            out.extend(res)
        else:
            print(f"    ⚠ {label} failed for chunk {i + 1}/{len(chunks)} "
                  f"({len(chunk)} rows) — leaving blank: {err}")
            out.extend(blank(chunk))
    return out


# ---------------------------------------------------------------------------
# Checkpoints — never re-pay an API for work already done
# ---------------------------------------------------------------------------
# A checkpoint is a local JSONL file (one record per line) written the instant
# each paid API result comes back. Local disk is instant and free, so we save
# there DURING the run and push to the Sheet incrementally (per result, or at the
# milestones below for batched column writes). If the run dies midway (crash,
# credit-exhaustion, Ctrl-C), the checkpoint still holds everything completed —
# the next run loads it, skips those items, and only pays for what's missing.
# Keyed by a stable id (e.g. company name) so re-runs dedup.

# By default checkpoints live under the current workspace's .cache/. That is fine
# for a single machine, but breaks when a run happens in a *fresh* workspace each
# time (e.g. Conductor spins up a new git worktree/container per run): the new
# workspace starts with an empty .cache/, so every re-run re-pays for searches the
# previous run already completed. Point GTM_CHECKPOINT_DIR at a stable path that
# outlives the workspace (a shared/mounted dir, or an absolute path in $HOME) and
# checkpoints then persist across runs regardless of where the workspace lives.
_CHECKPOINT_DIR_ENV = os.environ.get("GTM_CHECKPOINT_DIR")
CHECKPOINT_DIR = (
    os.path.abspath(os.path.expanduser(_CHECKPOINT_DIR_ENV))
    if _CHECKPOINT_DIR_ENV
    else os.path.join(os.getcwd(), ".cache", "checkpoints")
)


def checkpoint_path(name: str) -> str:
    """Return the on-disk path for a checkpoint, sanitising ``name`` to a safe file."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "checkpoint"
    return os.path.join(CHECKPOINT_DIR, f"{safe}.jsonl")


def checkpoint_append(path: str, key: str, value) -> None:
    """Append one ``{key, value}`` record. O(1) — no rewrite of prior entries."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"key": key, "value": value}, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())  # survive a hard kill, not just a clean exit


def checkpoint_load(path: str) -> Dict[str, object]:
    """Load a checkpoint into a ``{key: value}`` dict (last write wins per key)."""
    out: Dict[str, object] = {}
    if not os.path.exists(path):
        return out
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue  # tolerate a torn final line from a hard kill mid-write
            if isinstance(rec, dict) and "key" in rec:
                out[rec["key"]] = rec.get("value")
    return out


# ---------------------------------------------------------------------------
# Milestone flushing — push partial results to the Sheet DURING a run
# ---------------------------------------------------------------------------
# The JSONL checkpoint (above) is the crash-safety net, but the Sheet is the
# actual deliverable and it stays empty until the end-of-run batched write. If
# the checkpoint file is lost/clobbered, or the final write itself dies, a long
# run leaves nothing usable in the Sheet. Milestone flushing pushes partial
# results to the Sheet as the run progresses, so a big run is durable in the
# Sheet too — without a Sheets call per item (which would risk 429s when several
# workflow runs hit the same spreadsheet at once).
#
# Cadence: a batch larger than ``small_threshold`` rows flushes at the 25/50/75/
# 100% marks; a smaller batch just at halves (50/100%). The full count is always
# the final mark, so the last flush writes everything.

def flush_milestones(total: int, small_threshold: int = 200) -> List[int]:
    """Completed-counts at which to flush a batch of ``total`` items to the Sheet."""
    if total <= 0:
        return []
    fractions = (0.25, 0.5, 0.75, 1.0) if total > small_threshold else (0.5, 1.0)
    return sorted({max(1, math.ceil(total * f)) for f in fractions} | {total})


class MilestoneFlusher:
    """Fire ``flush_fn(done)`` once each 25%/50% milestone of ``total`` is reached.

    ``tick()`` is called on the MAIN THREAD (from a ``map_rate_limited`` on_result
    callback) as each item finishes; it triggers ``flush_fn`` when a milestone is
    crossed. ``flush_fn`` must write everything completed so far — it is a full
    (idempotent) re-write of the partial result, so a later flush supersedes an
    earlier one and a mid-run crash still leaves the last milestone in the Sheet.
    """

    def __init__(self, total: int, flush_fn, small_threshold: int = 200):
        self.total = total
        self._flush_fn = flush_fn
        self._pending = flush_milestones(total, small_threshold)
        self.done = 0

    def tick(self, n: int = 1) -> None:
        self.done += n
        if self._pending and self.done >= self._pending[0]:
            # Collapse every milestone we've passed into a single flush.
            while self._pending and self.done >= self._pending[0]:
                self._pending.pop(0)
            self._flush_fn(self.done)


# ---------------------------------------------------------------------------
# Row backends — same interface for a Google Sheet and a local CSV
# ---------------------------------------------------------------------------
# Both expose:
#   read_all()                        → list of rows (header + data)
#   write_header(col_idx, name)       → write one column header
#   write_cell(row_num, col_idx, val) → write one cell (row_num is 1-based, 1=header)
#   write_column(col_idx, values)     → write data rows 2..N+1 in a column

class GoogleSheetsBackend:
    def __init__(self, sheet_id: str, sheet_name: str):
        self.sheet_id = sheet_id
        self.sheet_name = sheet_name

    def read_all(self) -> List[List[str]]:
        return gws_read_sheet(self.sheet_id, self.sheet_name)

    def write_header(self, col_idx: int, name: str) -> None:
        gws_write_range(self.sheet_id, f"{self.sheet_name}!{col_letter(col_idx)}1", [[name]])

    def write_cell(self, row_num: int, col_idx: int, value: str) -> None:
        gws_write_range(self.sheet_id, f"{self.sheet_name}!{col_letter(col_idx)}{row_num}", [[value]])

    def write_column(self, col_idx: int, values: List[str]) -> None:
        if not values:
            return
        ltr = col_letter(col_idx)
        rng = f"{self.sheet_name}!{ltr}2:{ltr}{len(values) + 1}"
        gws_write_range(self.sheet_id, rng, [[v] for v in values])

    def write_row(self, row_num: int, updates: Dict[int, str], base_row: List[str]) -> None:
        """Persist every cell in ``updates`` (col_idx → value) for ``row_num`` in a
        SINGLE Sheets write, instead of one API call per column. Writes one
        contiguous range spanning the touched columns; any untouched cell inside
        that span is re-sent from ``base_row`` so it isn't clobbered."""
        if not updates:
            return
        lo, hi = min(updates), max(updates)
        vals = [updates.get(idx, base_row[idx] if idx < len(base_row) else "")
                for idx in range(lo, hi + 1)]
        rng = f"{self.sheet_name}!{col_letter(lo)}{row_num}:{col_letter(hi)}{row_num}"
        gws_write_range(self.sheet_id, rng, [vals])


class CsvBackend:
    """In-memory rows; rewrites the output CSV after every write so partial progress survives crashes."""

    def __init__(self, input_path: str, output_path: Optional[str] = None):
        self.input_path = input_path
        self.output_path = output_path or input_path
        self.rows: List[List[str]] = []

    def read_all(self) -> List[List[str]]:
        if not os.path.exists(self.input_path):
            raise FileNotFoundError(f"Input CSV not found: {self.input_path}")
        with open(self.input_path, newline="", encoding="utf-8") as f:
            self.rows = [list(row) for row in csv.reader(f)]
        return self.rows

    def _ensure(self, row_idx: int, col_idx: int) -> None:
        while len(self.rows) <= row_idx:
            self.rows.append([])
        while len(self.rows[row_idx]) <= col_idx:
            self.rows[row_idx].append("")

    def _flush(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.output_path)) or ".", exist_ok=True)
        width = max((len(r) for r in self.rows), default=0)
        for r in self.rows:
            while len(r) < width:
                r.append("")
        with open(self.output_path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(self.rows)

    def write_header(self, col_idx: int, name: str) -> None:
        self._ensure(0, col_idx)
        self.rows[0][col_idx] = name
        self._flush()

    def write_cell(self, row_num: int, col_idx: int, value: str) -> None:
        idx = row_num - 1  # row_num is 1-based
        self._ensure(idx, col_idx)
        self.rows[idx][col_idx] = value
        self._flush()

    def write_column(self, col_idx: int, values: List[str]) -> None:
        for i, v in enumerate(values):
            idx = i + 1  # data starts at row index 1 (sheet row 2)
            self._ensure(idx, col_idx)
            self.rows[idx][col_idx] = v
        self._flush()

    def write_row(self, row_num: int, updates: Dict[int, str], base_row: List[str]) -> None:
        """Apply every cell in ``updates`` for ``row_num`` then rewrite the CSV
        once, instead of a full rewrite per column. ``base_row`` is unused (rows
        are edited in place by index)."""
        if not updates:
            return
        idx = row_num - 1
        for col_idx, value in updates.items():
            self._ensure(idx, col_idx)
            self.rows[idx][col_idx] = value
        self._flush()


# ---------------------------------------------------------------------------
# Column mapping — reconcile arbitrary sheet headers with workflow fields
# ---------------------------------------------------------------------------

def cell_combined(row: List[str], indices: List[int]) -> str:
    """Join values from multiple columns into one string (skips empty/missing)."""
    parts = [row[i].strip() for i in indices if i < len(row) and row[i] and row[i].strip()]
    return " ".join(parts)


def get_or_create_col(headers: List[str], mapping: Dict[str, List[int]],
                      field_key: str, default_name: str) -> int:
    """Use the column ``field_key`` was mapped to if any; else append ``default_name``.
    Guards against the LLM returning out-of-bounds indices."""
    indices = [i for i in (mapping.get(field_key) or [])
               if isinstance(i, int) and 0 <= i < len(headers)]
    if indices:
        return indices[0]
    return ensure_col(headers, default_name)


def detect_columns(headers: List[str], sample_row: List[str],
                   required_fields: Dict[str, str],
                   client: "anthropic.Anthropic", max_tokens: int = 600) -> Dict[str, List[int]]:
    """Use Claude to map sheet headers to standard workflow fields.

    ``required_fields`` is {field_name: description}. Returns {field_name:
    [col_idx, ...]}; multiple indices means the values should be combined (e.g.
    firstName + lastName for "name"). Empty list means not found.
    """
    padded = list(sample_row) + [""] * max(0, len(headers) - len(sample_row))
    sample_block = "\n".join(
        f"  col {i} ({h}): {(padded[i] or '')[:80]}"
        for i, h in enumerate(headers)
    )
    fields_block = "\n".join(f"- {name}: {desc}" for name, desc in required_fields.items())

    prompt = f"""Map spreadsheet columns to standard workflow fields.

Sheet columns (index: header — sample value):
{sample_block}

Required fields:
{fields_block}

For each required field, return the column INDEX(es) whose contents best match.
- If a field spans multiple columns (e.g., name split into firstName + lastName), return all relevant indices.
- Headers don't have to match field names exactly — judge by content. Use the sample to disambiguate.
- Return null for fields that have no matching column.

Return JSON: {{"<field_name>": [<idx>, ...] or null, ...}}
Only valid JSON, no explanation."""

    resp = client.messages.create(
        model=CLAUDE_MODEL, temperature=0, max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    parsed = json.loads(strip_json_fence(resp.content[0].text))
    return {
        k: (v if isinstance(v, list) else [v] if v is not None else [])
        for k, v in parsed.items()
    }


# ---------------------------------------------------------------------------
# LinkedIn post-scraping config (read from context.md, scraper defaults as fallback)
# ---------------------------------------------------------------------------

DEFAULT_MAX_POSTS = 15
DEFAULT_DAYS_BACK = 90


def parse_post_config(icp_context: str) -> Dict:
    """Read post-scraping config from the ICP markdown, falling back to defaults."""
    max_posts = DEFAULT_MAX_POSTS
    days_back = DEFAULT_DAYS_BACK
    for line in icp_context.splitlines():
        ll = line.lower()
        if "max posts per profile" in ll:
            m = re.search(r"\d+", line)
            if m:
                max_posts = int(m.group())
        elif "days back" in ll:
            m = re.search(r"\d+", line)
            if m:
                days_back = int(m.group())
    return {"max_posts": max_posts, "days_back": days_back}
