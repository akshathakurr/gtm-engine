"""
Central config for GTM Engine.

Single source of truth for repo paths, the Claude model, and env-var-backed
credentials. Import this from any scraper or workflow:

    from config import CLAUDE_MODEL, APIFY_TOKEN, CONTEXT_DIR

Repo layout:
    context/     — flat .md files describing ICP, product, etc.
    scrapers/    — one folder per data source (snake_case)
    workflows/   — one folder per GTM play (snake_case)
"""

import os
from dotenv import load_dotenv

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

# Which context folder workflows read. Defaults to the top-level context/ folder.
# For multiple projects, keep each project's context.md in its own subfolder
# (e.g. context/luffy/) and point workflows at it by setting GTM_CONTEXT_DIR —
# either relative to the repo root ("context/luffy") or an absolute path.
_context_override = os.getenv("GTM_CONTEXT_DIR")
if _context_override:
    CONTEXT_DIR = _context_override if os.path.isabs(_context_override) \
        else os.path.join(REPO_ROOT, _context_override)
else:
    CONTEXT_DIR = os.path.join(REPO_ROOT, "context")
SCRAPERS_DIR = os.path.join(REPO_ROOT, "scrapers")
WORKFLOWS_DIR = os.path.join(REPO_ROOT, "workflows")

load_dotenv(os.path.join(REPO_ROOT, ".env"), override=True)

CLAUDE_MODEL = "claude-sonnet-4-6"

APIFY_TOKEN = os.getenv("APIFY_API_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
APOLLO_API_KEY = os.getenv("APOLLO_API_KEY")
EXA_API_KEY = os.getenv("EXA_API_KEY")
PARALLEL_API_KEY = os.getenv("PARALLEL_API_KEY")


def cached_system(*blocks: str):
    """Build a Claude ``system=`` param whose static text is prompt-cached.

    Returns a list of text blocks with a ``cache_control`` breakpoint on the last
    non-empty one, so the entire prefix (all blocks up to and including it) is
    cached server-side and re-read at ~10% of the input-token price by later calls
    sharing the same prefix. Pass the invariant parts only (system prompt,
    ICP/context) — never per-lead data, which belongs in ``messages`` so the cache
    key stays stable. Below Anthropic's ~1024-token minimum the API simply doesn't
    cache (no error), so this is always safe to use.

    Lives in config (imported by both skills/ and workflows/) so neither package
    has to reach into the other for it.
    """
    out = []
    for b in blocks:
        b = (b or "").strip()
        if b:
            out.append({"type": "text", "text": b})
    if out:
        out[-1]["cache_control"] = {"type": "ephemeral"}
    return out
