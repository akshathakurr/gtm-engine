"""Shared core for the copy-writer skills.

``email_copy_writer`` and ``linkedin_copy_writer`` run the same three-call
pipeline (signal extraction → draft with self-review → conditional repair) and
differ only in their prompts and a few knobs. Those shared mechanics used to be
copy-pasted between the two ~480-line skills; they live here once so a fix lands
in both channels at the same time.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from anthropic import Anthropic

from config import ANTHROPIC_API_KEY, CLAUDE_MODEL

client = Anthropic(api_key=ANTHROPIC_API_KEY)

MAX_POSTS_IN_PROMPT = 5
MAX_POST_CHARS = 500


def extract_json(text: str) -> Optional[Dict[str, Any]]:
    """Parse the JSON object out of a model reply, tolerating a code fence or
    prose around it. Returns None if nothing parseable is found."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def leading_token(value: Any) -> str:
    """First word of a review answer, lowercased. The model often appends a
    justification ("no — opens with a direct quote..."); we gate on the verdict,
    not the prose."""
    return re.split(r"[^a-z]+", str(value or "").strip().lower(), maxsplit=1)[0]


def extract_signals(system: str, lead_data_block: str, icp_context: str) -> Dict[str, Any]:
    """Call 1 of the pipeline: surface the 1-2 strongest signals from the data."""
    user_msg = (
        f"## Sender Context\n{icp_context.strip() or '(none)'}\n\n"
        f"## All Lead Data\n{lead_data_block}\n\n"
        "Identify the 1-2 strongest signals. Return only the JSON object."
    )
    resp = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=500,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
    )
    text = "".join(b.text for b in resp.content if hasattr(b, "text"))
    return extract_json(text) or {"signals": [], "strength": "none", "notes": ""}


def build_lead_data_block(
    name: str,
    company: str,
    position: str,
    buyer_persona: str,
    priority: str,
    matching_posts: List[Dict[str, Any]],
    small_talk: str,
    personalisation_hook: str,
    employee_count: str,
    est_revenue: str,
    total_funding: str,
    hq: str,
    competitors: str,
    email: Optional[str] = None,
) -> str:
    """Render every known fact about a lead into the prompt's data block.
    ``email`` is included only for channels that have it (cold email)."""
    parts = [
        f"Name: {name}",
        f"Company: {company}",
        f"Position: {position or '(unknown)'}",
    ]
    if email is not None:
        parts.append(f"Email: {email or '(unknown)'}")
    parts += [
        f"Buyer persona: {buyer_persona or '(unknown)'}",
        f"Priority: {priority or '(unknown)'}",
        f"Employee count: {employee_count or '(unknown)'}",
        f"Est revenue: {est_revenue or '(unknown)'}",
        f"Total funding: {total_funding or '(unknown)'}",
        f"HQ: {hq or '(unknown)'}",
        f"Competitors: {competitors or '(none)'}",
    ]
    block = "\n".join(parts)

    if small_talk and small_talk.strip():
        block += f"\n\nSmall talk / personal signals:\n{small_talk.strip()}"

    if personalisation_hook and personalisation_hook.strip():
        block += f"\n\nPre-researched hooks (angles):\n{personalisation_hook.strip()}"

    if matching_posts:
        posts_lines = []
        for p in matching_posts[:MAX_POSTS_IN_PROMPT]:
            text = (p.get("text") or "").strip()
            if len(text) > MAX_POST_CHARS:
                text = text[:MAX_POST_CHARS].rstrip() + "…"
            date = (p.get("posted_at") or "")[:10]
            posts_lines.append(f"[{date}] {text}" if date else text)
        block += "\n\nMatching LinkedIn posts:\n" + "\n\n".join(posts_lines)

    return block


def audit_copy(parsed: Dict[str, Any], check_banned: bool = False) -> List[str]:
    """Extract self-review violations from a draft. Returns issue strings.

    If the model omitted the ``review`` block entirely we can't audit it, so we
    return no issues (rather than flag all three and trigger a wasted repair)."""
    review = parsed.get("review") or {}
    if not review:
        return []
    issues = []
    if leading_token(review.get("mass_sent_feel")) != "no":
        issues.append("feels mass-sent")
    if leading_token(review.get("would_hook_reply")) != "yes":
        issues.append("weak hook")
    if leading_token(review.get("reads_human")) != "yes":
        issues.append("not human")
    if check_banned:
        banned = review.get("banned_phrases_used") or []
        if banned:
            issues.append(f"banned phrases used: {', '.join(str(p) for p in banned)}")
    return issues


def repair_copy(system: str, prompt: str, draft: str, violations: List[str],
                max_tokens: int) -> Dict[str, Any]:
    """One repair call: send the draft + violations back and ask for a fix."""
    violation_list = "\n".join(f"- {v}" for v in violations)
    repair_msg = (
        f"{prompt}\n\n"
        "---\n\n"
        f"FIRST DRAFT (needs revision):\n{draft}\n\n"
        f"SELF-REVIEW VIOLATIONS:\n{violation_list}\n\n"
        "Fix each violation. Return the corrected copy in the same JSON format."
    )
    try:
        resp = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": repair_msg}],
        )
        text = "".join(b.text for b in resp.content if hasattr(b, "text"))
        return extract_json(text) or {"copy": draft, "review": {}, "signal_used": "", "errors": []}
    except Exception as e:
        return {"copy": draft, "review": {}, "signal_used": "", "errors": [f"repair_failed: {e}"]}
