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
import json
import shutil
import subprocess
import sys
import tempfile
from dotenv import load_dotenv

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

# Load .env BEFORE reading any env-var below. GTM_CONTEXT_DIR in particular must
# be resolved after this: reading it first (the old order) meant a value set in
# .env was silently ignored, so multi-project runs fell back to the default
# context/ folder even when a subfolder was requested.
load_dotenv(os.path.join(REPO_ROOT, ".env"), override=True)

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

# Surface which context is in use so a wrong/fallback context is visible instead
# of silently personalising against the wrong project.
print(f"[context] using {os.path.relpath(CONTEXT_DIR, REPO_ROOT)}"
      f"{' (default; set GTM_CONTEXT_DIR to override)' if not _context_override else ''}",
      file=sys.stderr)

CLAUDE_MODEL = os.environ.get("GTM_MODEL") or "claude-sonnet-5"

# Default reasoning effort for each subscription CLI backend. Both are
# overridable per-run without touching code.
GTM_CLAUDE_EFFORT = os.environ.get("GTM_CLAUDE_EFFORT") or "high"
GTM_CODEX_MODEL_DEFAULT = os.environ.get("GTM_CODEX_MODEL") or "gpt-5.6-luna"
GTM_CODEX_EFFORT = os.environ.get("GTM_CODEX_EFFORT") or "xhigh"

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


# ---------------------------------------------------------------------------
# Brain backend — API key vs. subscription CLI (GTM_BRAIN)
# ---------------------------------------------------------------------------
#
# Every workflow does its "thinking" (scoring, classifying, copywriting) by
# calling Claude. By default that's the Anthropic API, billed against
# ANTHROPIC_API_KEY. Set GTM_BRAIN to route those calls through a subscription
# instead — no API key spent:
#
#     GTM_BRAIN=api     → Anthropic API via anthropic.Anthropic()
#     GTM_BRAIN=claude  → `claude -p` CLI, billed to your Claude plan
#     GTM_BRAIN=codex   → `codex exec` CLI, billed to your ChatGPT plan
#
# When GTM_BRAIN is unset the default is HOST-AWARE (_detect_default_brain):
# opened inside a Codex session → codex; inside Claude Code / the Claude
# desktop app → claude; anywhere else → api. If an auto-detected CLI isn't
# usable: a Codex host never falls back — it stops and tells the user to switch
# the session to full-access mode (keeping the run on their ChatGPT plan); a
# Claude host falls back (codex CLI → API key → error). An explicit GTM_BRAIN
# never falls back — it hard-stops with the preflight's fix instructions.
#
# The CLI backends return a tiny object that quacks exactly like an Anthropic
# SDK response (`resp.content[0].text`), so the ~35 existing call sites don't
# change — only the client factory does. Use it like:
#
#     GTM_BRAIN=claude python3 -m workflows.email_outreach.workflow --sheet-id …
#
# One-time setup for the `claude` backend: run `claude setup-token` to mint a
# subscription-backed token, and make sure `claude auth status` shows an OAuth /
# subscription method (not "api_key"). We strip ANTHROPIC_API_KEY from the CLI's
# environment on every call so a stray env key can't silently bill the API.
#
# Known limitations of the CLI backends (documented, not bugs):
#   - temperature / max_tokens can't be set via the CLI, so runs are a touch less
#     deterministic than the API path's temperature=0.
#   - prompt caching (cached_system) doesn't apply on the CLI path.
#   - each call spawns a full CLI process → slower than an API call, and draws
#     down the plan's usage window. Fine for consulting-batch scale.

def _detect_default_brain() -> str:
    """Host-aware default when GTM_BRAIN is unset.

    Opened inside a Codex session (Codex app / codex exec) → `codex`; inside
    Claude Code (CLI or Claude desktop app) → `claude`; otherwise, or when an
    ANTHROPIC_API_KEY is configured and no known host is detected → `api`.
    Codex is checked first: a codex shell spawned from Claude Code inherits the
    outer CLAUDE* vars too, and the innermost host is the one the user is in.
    An explicit GTM_BRAIN always wins over detection.
    """
    if os.getenv("CODEX_THREAD_ID") or os.getenv("CODEX_SANDBOX") or os.getenv("CODEX_CI"):
        return "codex"
    if os.getenv("CLAUDECODE") or os.getenv("CLAUDE_CODE_ENTRYPOINT"):
        return "claude"
    return "api"


_EXPLICIT_BRAIN = (os.getenv("GTM_BRAIN") or "").strip().lower()
BRAIN_MODE = _EXPLICIT_BRAIN or _detect_default_brain()
# True when BRAIN_MODE came from host detection rather than an explicit
# GTM_BRAIN — only auto-detected modes are allowed to fall back on preflight
# failure (see make_brain_client; Codex hosts still never fall back).
BRAIN_MODE_AUTODETECTED = not _EXPLICIT_BRAIN


class _TextBlock:
    """Mimics anthropic's content block: exposes `.text`."""
    __slots__ = ("text", "type")

    def __init__(self, text: str):
        self.text = text
        self.type = "text"


class _BrainResponse:
    """Duck-types an anthropic Message: `resp.content[0].text` works."""

    def __init__(self, text: str):
        self.content = [_TextBlock(text)]
        self.stop_reason = "end_turn"


def _flatten_system(system) -> str:
    """Collapse a `system=` value (plain str or cached_system block list) to text."""
    if not system:
        return ""
    if isinstance(system, str):
        return system
    parts = []
    for b in system:
        if isinstance(b, dict):
            parts.append(b.get("text", ""))
        elif isinstance(b, str):
            parts.append(b)
    return "\n\n".join(p for p in parts if p)


def _flatten_messages(messages) -> str:
    """Collapse a `messages=` list into a single prompt string.

    Every call site in this repo sends one user message with string content, but
    handle multi-message / list-content shapes defensively so nothing silently
    drops."""
    parts = []
    for m in messages or []:
        content = m.get("content", "")
        if isinstance(content, list):
            content = "\n".join(
                c.get("text", "") if isinstance(c, dict) else str(c) for c in content
            )
        role = m.get("role", "user")
        parts.append(content if role == "user" else f"[{role}]\n{content}")
    return "\n\n".join(p for p in parts if p)


def _extract_claude_result(stdout: str) -> str:
    """Pull the assistant text out of `claude -p --output-format json` output.

    That output is a JSON array of events; the final `type == "result"` event
    carries the answer in its `result` field (and an `is_error` flag)."""
    data = json.loads(stdout)
    if isinstance(data, list):
        results = [x for x in data if isinstance(x, dict) and x.get("type") == "result"]
        if not results:
            raise RuntimeError("claude CLI returned no result event")
        r = results[-1]
        if r.get("is_error"):
            raise RuntimeError(f"claude CLI error: {r.get('result') or r.get('subtype')}")
        return r.get("result", "")
    if isinstance(data, dict):
        if data.get("is_error"):
            raise RuntimeError(f"claude CLI error: {data.get('result')}")
        return data.get("result", "")
    raise RuntimeError("unexpected claude CLI output shape")


def _claude_preflight() -> None:
    """Hard-stop if `claude` would bill the API instead of the subscription.

    GTM_BRAIN=claude is meant to spend the Claude *subscription*, no API key. But
    the CLI re-injects an ANTHROPIC_API_KEY pinned in ~/.claude/settings.json (its
    `env` block) or the shell profile even after we strip it from the subprocess
    env — so it would silently bill the API. Check the CLI's own auth report (with
    the env key stripped, matching how we invoke it) and refuse to proceed if it
    still resolves to `api_key`, telling the user exactly how to go keyless.
    Never blocks on an indeterminate result — only on a confirmed api_key auth."""
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    try:
        proc = subprocess.run(
            ["claude", "auth", "status"], capture_output=True, text=True,
            env=env, stdin=subprocess.DEVNULL, timeout=30,
        )
        info = json.loads(proc.stdout)
    except Exception:
        return  # can't determine — let the actual run surface any real error
    if isinstance(info, dict) and info.get("authMethod") == "api_key":
        src = info.get("apiKeySource") or "unknown"
        raise RuntimeError(
            f"GTM_BRAIN=claude, but the Claude CLI is authenticated via an "
            f"ANTHROPIC_API_KEY (source: {src}) — running would BILL THE API, not "
            "your subscription. To go keyless: (1) run `claude setup-token` to "
            "mint a subscription token, then (2) remove the ANTHROPIC_API_KEY line "
            "from ~/.claude/settings.json (its \"env\" block) and your shell "
            "profile (e.g. ~/.zshrc). Re-run once `claude auth status` shows an "
            "oauth/subscription method."
        )
    # An org/Console login (`/login` with a work account) reports
    # authMethod "claude.ai" but bills the ORG's API credits through a managed
    # key — the subscriptionType field is null in that case. A setup-token in
    # CLAUDE_CODE_OAUTH_TOKEN overrides it (authMethod becomes "oauth_token"),
    # so only stop when there's no token AND the login is confirmed org-billed.
    if (
        isinstance(info, dict)
        and info.get("authMethod") == "claude.ai"
        and "subscriptionType" in info
        and info.get("subscriptionType") is None
        and not env.get("CLAUDE_CODE_OAUTH_TOKEN")
    ):
        org = info.get("orgName") or "your organization"
        raise RuntimeError(
            f"GTM_BRAIN=claude, but the Claude CLI is logged in with a Console/"
            f"org account ({org}) that has no subscription attached — running "
            "would bill that org's API credits through its managed key, not a "
            "Claude plan. Fix: run `claude setup-token` with the account that "
            "holds the Pro/Max plan and put the token in .env as "
            "CLAUDE_CODE_OAUTH_TOKEN=<token>. Re-run once `claude auth status` "
            "shows authMethod \"oauth_token\" (with the token in the env)."
        )


def _run_claude_cli(system: str, prompt: str, model: str, timeout: int = 300) -> str:
    cmd = ["claude", "-p", "--output-format", "json"]
    if model:
        cmd += ["--model", model]
    # Default to high reasoning effort on the subscription — override with
    # GTM_CLAUDE_EFFORT (low/medium/high/xhigh/max) if a run needs less.
    cmd += ["--effort", GTM_CLAUDE_EFFORT]
    # Force subscription/OAuth auth: never let an env API key be the source.
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)

    sys_file = None
    try:
        if system:
            sys_file = tempfile.NamedTemporaryFile(
                "w", suffix=".txt", delete=False, encoding="utf-8"
            )
            sys_file.write(system)
            sys_file.close()
            cmd += ["--system-prompt-file", sys_file.name]
        proc = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True, env=env, timeout=timeout,
        )
    finally:
        if sys_file:
            try:
                os.unlink(sys_file.name)
            except OSError:
                pass
    if proc.returncode != 0:
        raise RuntimeError(f"claude CLI failed (exit {proc.returncode}): {proc.stderr[:400]}")
    return _extract_claude_result(proc.stdout)


def _codex_binary() -> str:
    """The codex executable name (first token of GTM_CODEX_CMD, default 'codex')."""
    return (os.getenv("GTM_CODEX_CMD", "codex exec").split() or ["codex"])[0]


def _codex_preflight() -> None:
    """Fail fast (with a clear cause) if the codex binary can't run here.

    On macOS the codex binary sandboxes itself; when `codex exec` is spawned from
    inside another Codex agent session (or a supervised/sandboxed shell) the
    nested process is killed with SIGKILL (-9) before it does any work. Detect
    that once, up front, so a run stops with an actionable message instead of
    crashing mid-column-mapping with an opaque `exit -9`."""
    # Inside a SANDBOXED codex session (CODEX_SANDBOX=seatbelt) a nested codex
    # can't initialize its app-server ("Operation not permitted") even though
    # `codex --version` succeeds — so check the env marker, not just the binary.
    # In full-access mode the var is unset and nesting works (verified).
    if os.getenv("CODEX_SANDBOX"):
        raise RuntimeError(
            "GTM_BRAIN=codex inside a sandboxed Codex session: a nested codex "
            "can't start under the sandbox. Fixes: switch this Codex session to "
            "full-access mode, or use GTM_BRAIN=claude, or run from a plain "
            "terminal / Claude Code / Conductor."
        )
    try:
        proc = subprocess.run(
            [_codex_binary(), "--version"], capture_output=True, text=True,
            stdin=subprocess.DEVNULL, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"codex CLI is not runnable ({e}). Install it or set GTM_CODEX_CMD.")
    if proc.returncode != 0:
        rc = proc.returncode
        why = (f"killed by signal {-rc}" if rc < 0 else f"exit {rc}")
        raise RuntimeError(
            f"codex CLI can't run in this environment ({why}). Codex sandboxes "
            "itself and is SIGKILLed when nested inside another Codex/agent "
            "session or a supervised shell. Run the workflow from a plain "
            "terminal, or use GTM_BRAIN=claude (Claude subscription)."
        )


def _run_codex_cli(system: str, prompt: str, model: str, timeout: int = 300) -> str:
    """Route a call through the Codex CLI (ChatGPT plan).

    Codex exec has no system-prompt flag, so system + prompt are concatenated. We
    capture the answer via `--output-last-message` (a temp file holding ONLY the
    final assistant message) rather than scraping stdout — stdout also carries
    reasoning/tool framing, which would corrupt the JSON that workflows parse.
    `--skip-git-repo-check` avoids stalling outside a repo; `codex exec` is already
    non-interactive so no approval flag is needed. We run with `--sandbox
    read-only`: the adapter only needs codex to READ the prompt and reply, never to
    touch the filesystem or run commands — and prompts carry scraped third-party
    content (LinkedIn posts, web pages), so a read-only sandbox denies any injected
    instruction the ability to act. Override the base command via GTM_CODEX_CMD if
    the installed version's flags differ.

    The incoming `model` is an Anthropic id (CLAUDE_MODEL) meaningless to codex —
    passing it as `-m` makes codex exit 1 ("model metadata not found"). So we
    IGNORE it and default to GTM_CODEX_MODEL_DEFAULT (gpt-5.6-luna unless
    overridden via GTM_CODEX_MODEL) at GTM_CODEX_EFFORT reasoning effort (xhigh
    unless overridden via GTM_CODEX_EFFORT), rather than whatever this machine's
    global ~/.codex/config.toml happens to default to — so gtm-engine's model
    choice doesn't silently drift with unrelated Codex config changes."""
    full = f"{system}\n\n{prompt}" if system else prompt
    base = os.getenv("GTM_CODEX_CMD", "codex exec").split()
    out_file = tempfile.NamedTemporaryFile("r", suffix=".txt", delete=False, encoding="utf-8")
    out_file.close()
    # Run in an empty scratch dir, not the repo root: with no -C, codex's
    # read-only sandbox still has read access to everything in the repo (git
    # config, CLAUDE.md, README, .env), which it can and will pull unprompted
    # facts from to fill any gap the prompt leaves blank. An empty tempdir
    # leaves nothing in reach beyond the prompt itself.
    scratch_dir = tempfile.mkdtemp(prefix="gtm-codex-brain-")
    cmd = base + [
        "--skip-git-repo-check",
        "--sandbox", "read-only",
        "-C", scratch_dir,
        "--output-last-message", out_file.name,
        "-m", GTM_CODEX_MODEL_DEFAULT,
        "-c", f"model_reasoning_effort={GTM_CODEX_EFFORT}",
    ]
    cmd += [full]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=timeout,
        )
        if proc.returncode != 0:
            rc = proc.returncode
            if rc < 0:
                detail = (
                    f"killed by signal {-rc} — codex is SIGKILLed when nested inside "
                    "another Codex/agent session; run from a plain terminal or use "
                    "GTM_BRAIN=claude"
                )
            elif "failed to initialize in-process app-server" in proc.stderr or \
                    "Operation not permitted" in proc.stderr:
                # Newer codex versions fail this way (instead of SIGKILL) when
                # nested inside another Codex session's sandbox (e.g. the Codex app).
                detail = (
                    f"exit {rc} — codex cannot start inside another Codex session's "
                    "sandbox (Codex app / nested codex). Fixes: switch the Codex "
                    "session to full-access mode (nested codex works unsandboxed, "
                    "verified), use GTM_BRAIN=claude here, or run from a plain "
                    f"terminal / Claude Code / Conductor. (stderr: {proc.stderr[:200]})"
                )
            else:
                detail = f"exit {rc}: {proc.stderr[:400]}"
            raise RuntimeError(f"codex CLI failed ({detail})")
        with open(out_file.name, encoding="utf-8") as f:
            text = f.read().strip()
        return text or proc.stdout.strip()
    finally:
        try:
            os.unlink(out_file.name)
        except OSError:
            pass
        shutil.rmtree(scratch_dir, ignore_errors=True)


class _CliMessages:
    def __init__(self, backend: str):
        self.backend = backend

    def create(self, *, model=None, system=None, messages=None, **_ignored):
        sys_txt = _flatten_system(system)
        prompt = _flatten_messages(messages)
        model = model or CLAUDE_MODEL
        if self.backend == "claude":
            text = _run_claude_cli(sys_txt, prompt, model)
        else:
            text = _run_codex_cli(sys_txt, prompt, model)
        return _BrainResponse(text)


class _CliBrainClient:
    """Drop-in stand-in for anthropic.Anthropic() backed by a subscription CLI."""

    def __init__(self, backend: str):
        self.messages = _CliMessages(backend)


def _api_client():
    """The real Anthropic SDK client, with bounded per-request timeouts.

    The SDK default is a 600s timeout, so a single hung connection stalls a
    worker thread for up to 10 minutes before recovering — across a concurrent
    per-company fan-out that reads as the whole run wedging. A tight timeout
    plus a few retries (the SDK backs off between them) turns a hung socket
    into a fast retry."""
    import anthropic
    return anthropic.Anthropic(
        timeout=float(os.environ.get("ANTHROPIC_TIMEOUT") or 60),
        max_retries=int(os.environ.get("ANTHROPIC_MAX_RETRIES") or 4),
    )


def _autodetect_fallback(mode: str, preflight_error: RuntimeError):
    """Fallback ladder when an AUTO-DETECTED CLI backend fails preflight.

    Auto-detected mode is a convenience, not a demand — but each host falls
    back differently:
      - Codex host → NO fallback: re-raise so the user sees the preflight's
        "switch this session to full-access mode" fix and the run stays on
        their ChatGPT plan, instead of silently billing another brain.
      - Claude host → try the codex CLI (still a subscription), then the API
        if a key is configured, then re-raise.
    Explicit GTM_BRAIN never reaches here (make_brain_client re-raises first).
    """
    if mode == "codex":
        raise preflight_error
    try:
        _codex_preflight()
        print(f"[GTM_BRAIN] auto-detected {mode} backend isn't usable here — "
              "falling back to the codex CLI (subscription). Set GTM_BRAIN "
              "explicitly to override.", file=sys.stderr)
        return _CliBrainClient("codex")
    except RuntimeError:
        pass
    if os.getenv("ANTHROPIC_API_KEY"):
        print(f"[GTM_BRAIN] auto-detected {mode} backend isn't usable here — "
              "falling back to the Anthropic API (key found). Set GTM_BRAIN "
              "explicitly to override.", file=sys.stderr)
        return _api_client()
    raise preflight_error


def make_brain_client():
    """Return the LLM client for the active GTM_BRAIN backend.

    `api` (default) → real anthropic.Anthropic(); `claude`/`codex` → a CLI-backed
    stand-in that spends a subscription instead of the API key. All three expose
    the same `.messages.create(...)` → `resp.content[0].text` interface."""
    mode = BRAIN_MODE
    if mode == "api":
        return _api_client()
    if mode in ("claude", "codex"):
        # Fail fast with an actionable cause instead of crashing mid-run: codex
        # gets SIGKILLed when nested; claude silently falls back to the API key.
        try:
            _claude_preflight() if mode == "claude" else _codex_preflight()
        except RuntimeError as err:
            # An EXPLICIT GTM_BRAIN keeps the hard stop — the user asked for
            # subscription billing, so failing beats surprise API spend.
            if not BRAIN_MODE_AUTODETECTED:
                raise
            return _autodetect_fallback(mode, err)
        auto = " (auto-detected from host)" if BRAIN_MODE_AUTODETECTED else ""
        print(f"[GTM_BRAIN={mode}]{auto} routing Claude calls through the "
              f"{'Claude' if mode == 'claude' else 'Codex'} CLI (no API key spent)",
              file=sys.stderr)
        return _CliBrainClient(mode)
    raise ValueError(f"GTM_BRAIN must be one of api|claude|codex, got {mode!r}")
