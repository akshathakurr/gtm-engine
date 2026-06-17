"""
Project Context skill.

The single place that turns the raw files under /context into one clean context
string for LLM prompts. Every other skill consumes data the workflow already
gathered; this is the exception — its whole job is to BE the context provider, so
it reads the context files itself.

Why it exists (vs. just concatenating the files):
- `context/context.md` is a questionnaire — each `## Section` wraps the real answer
  in `### Answer`, surrounded by `<!-- Question / Used by / Example -->` scaffolding
  and "(fill this in)" placeholders. Dumping it raw feeds the model a pile of
  instructions-to-itself instead of facts.
- Free-form files (`icp.md`, `competitors.md`) have no `### Answer` blocks — they're
  hand-written and should pass through as-is (minus HTML comments).

`get_context()` extracts just the real content from each file, drops empty/placeholder
sections, and labels each kept section by its header.

Workflow contract (called from linkedin_comment_helper):

    from skills.project_context import skill as project_context_skill
    ctx = project_context_skill.get_context()   # -> str (clean, labeled context)

Returns "" (never raises) when no usable context exists, so callers degrade
gracefully instead of crashing.
"""

from __future__ import annotations

import os
import re
from typing import List, Tuple

from config import CONTEXT_DIR

# Bodies that mean "the user left this blank" — skip them.
_PLACEHOLDERS = {"(fill this in)", "(none)", "(skip)", ""}


def _strip_comments(text: str) -> str:
    """Remove `<!-- ... -->` blocks — in the questionnaire these hold the
    Question / Used by / Example scaffolding, not real answers."""
    return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)


def _clean_body(body: str) -> str:
    """Drop comment lines and surrounding whitespace from a section body."""
    lines = [ln for ln in body.splitlines() if not ln.strip().startswith("<!--")]
    return "\n".join(lines).strip()


def _is_placeholder(body: str) -> bool:
    return body.strip().lower() in _PLACEHOLDERS


def _has_values(body: str) -> bool:
    """True if the body carries actual content, not just empty template labels.

    Catches unfilled templates like `- **Name:**` / `- **Website:**` (label, no
    value) so a blank `competitors.md` or progress tracker is skipped entirely.
    A line counts as a value if it has text after its last colon, or has no colon
    but is real prose.
    """
    for line in body.splitlines():
        stripped = line.strip().lstrip("-*").strip().replace("**", "")
        if not stripped:
            continue
        if ":" in stripped:
            if stripped.split(":", 1)[1].strip():
                return True
        elif stripped:
            return True
    return False


def _extract_sections(text: str) -> List[Tuple[str, str]]:
    """Return [(header, body), ...] of the real content in one file.

    Questionnaire format (`## Header` + `### Answer`): keep the `### Answer` body.
    Free-form format (no `### Answer` anywhere): keep each `## Header` section body
    as written.
    """
    questionnaire = re.search(r"(?m)^###\s+Answer\s*$", text) is not None
    sections: List[Tuple[str, str]] = []

    for m in re.finditer(r"(?ms)^##\s+(.+?)\s*$\n(.*?)(?=^##\s+|\Z)", text):
        header = m.group(1).strip()
        section = m.group(2)

        if questionnaire:
            ans = re.search(r"(?ms)^###\s+Answer\s*$\n(.*?)(?=^###\s+|\Z)", section)
            if not ans:
                continue
            body = _clean_body(ans.group(1))
        else:
            body = _clean_body(section)

        if not _is_placeholder(body) and _has_values(body):
            sections.append((header, body))

    return sections


def _format_file(text: str) -> str:
    """One file -> a clean block of labeled sections, or "" if nothing usable."""
    sections = _extract_sections(_strip_comments(text))
    if not sections:
        return ""
    return "\n\n".join(f"## {header}\n{body}" for header, body in sections)


def get_context(context_dir: str = CONTEXT_DIR) -> str:
    """Assemble all context files into one clean string for LLM prompts.

    Reads every non-`.example` `.md` file in `context_dir`, strips questionnaire
    scaffolding and empty placeholders, and joins the results. Returns "" if the
    directory is missing or holds no usable content.
    """
    if not os.path.isdir(context_dir):
        return ""

    blocks: List[str] = []
    for fname in sorted(os.listdir(context_dir)):
        if not fname.endswith(".md") or ".example" in fname:
            continue
        try:
            with open(os.path.join(context_dir, fname)) as f:
                raw = f.read()
        except OSError:
            continue
        block = _format_file(raw)
        if block:
            blocks.append(block)

    return "\n\n---\n\n".join(blocks)


# ---------------------------------------------------------------------------
# Run directly for testing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else CONTEXT_DIR
    ctx = get_context(target)
    if ctx:
        print(ctx)
    else:
        print(f"(no usable context found in {target})")
