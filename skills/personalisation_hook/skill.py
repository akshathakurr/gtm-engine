"""
Personalisation Hook skill.

Given everything the workflow knows about a lead — small talk, recent
LinkedIn posts, company firmographics, role, ICP context — produce 2-3
one-line **talking points** an SDR can hang a personalised message on.

This skill does NOT write outreach copy. It only surfaces angles. The copy
writer skill turns these angles into actual messages.

Workflow contract (called from email_outreach + linkedin_outreach Step 8/9):

    from skills.personalisation_hook import skill as hook_skill
    result = hook_skill.generate_hooks(
        name=..., company=..., position=...,
        matching_posts=[{"url":..., "text":..., "posted_at":...}, ...],
        small_talk="- F1 fan ...\\n- Coffee nerd ...",
        icp_context="<full context.md content>",
        # optional extras the workflow may pass:
        competitors=["Acme", "Globex"],
        company_description="...",
        employee_count="...", est_revenue="...", total_funding="...", hq="...",
    )
    # result == {"hooks": "- ...\\n- ...\\n- ...", "errors": []}

Design rules:
- Most weight goes to small_talk + matching_posts (specific, recent, real).
- Firmographics + competitors are secondary — only useful if they suggest
  a concrete angle (e.g. "you posted a sales role and we sell sales tooling").
- Never invent facts. Never paraphrase quotes in a way that changes meaning.
- Never write outreach copy ("Hi <name>, just saw..."). Only the angle.
- 2-3 bullets max. Empty string if nothing substantive exists.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from anthropic import Anthropic, APIConnectionError, InternalServerError, RateLimitError

from config import ANTHROPIC_API_KEY, CLAUDE_MODEL
from skills._copy_core import extract_json

_client = Anthropic(api_key=ANTHROPIC_API_KEY)

MAX_HOOKS = 3
MAX_POSTS_IN_PROMPT = 8
MAX_POST_CHARS = 600
_RETRYABLE = (RateLimitError, InternalServerError, APIConnectionError)


SYSTEM_PROMPT = """You are an SDR research assistant. Your job is to surface 2-3 SPECIFIC talking points an SDR can use as the angle for a personalised outreach message.

You are NOT writing the message. You are writing the *angle* the human will build the message around.

INPUT WEIGHTING (most → least important):
1. Small talk signals (humanizing details: hobbies, fandoms, quirks)
2. Matching LinkedIn posts (what the lead has been talking about lately)
3. Company-level signals only if they suggest a concrete angle tied to what we sell:
   - hiring signals (roles posted), competitor moves, recent funding/launches, headcount stage
4. Role/title — only as supporting context, never as a standalone hook

GOOD talking points (one line each):
- "Just posted about going to AWS re:Invent — open with a re:Invent reference."
- "Mentions yoga repeatedly on Twitter — natural opener around mindfulness/recovery."
- "Posted a Senior AE job last week — angle: 'doubling down on sales' (we sell sales tooling)."
- "Big Ferrari/F1 fan — open with a recent race reference."
- "Their main competitor [X] just raised $40M — ask how they're thinking about positioning."

BAD talking points (do NOT produce these):
- Writing the actual outreach: "Hi <name>, just saw your post about..."
- Generic openers: "Saw you're growing fast", "Hope this finds you well", "Loved your recent post"
- Restating titles: "VP of Sales at Acme" (not an angle)
- Vague platitudes: "passionate about AI", "building the future"
- Anything you can't trace to a specific input field

HARD RULES:
- 2-3 bullets MAX. If only 1 substantive angle exists, return 1. If 0, return empty.
- Each bullet is ONE line, plain text, starts with "- ".
- Reference the specific signal in the bullet (the post topic, the small-talk detail, the competitor name).
- Do not invent facts. If small_talk and posts are empty/weak, return empty.
- Do not write the opening message itself.
- Tie at least one hook to what WE sell (from icp_context) when there's a credible angle. If none, that's fine — humanizing hooks alone are valuable.

OUTPUT FORMAT (JSON only):
{
  "hooks": "- <one-line angle>\\n- <one-line angle>\\n- <one-line angle>",
  "errors": []
}

If nothing substantive exists:
{"hooks": "", "errors": ["no usable signal"]}
"""


def _trim_post(post: Dict[str, Any]) -> Dict[str, str]:
    text = (post.get("text") or "").strip()
    if len(text) > MAX_POST_CHARS:
        text = text[:MAX_POST_CHARS].rstrip() + "…"
    return {
        "url": post.get("url", ""),
        "posted_at": post.get("posted_at", ""),
        "text": text,
    }


def _build_user_payload(
    name: str,
    company: str,
    position: str,
    matching_posts: List[Dict[str, Any]],
    small_talk: str,
    icp_context: str,
    competitors: Optional[List[str]],
    company_description: str,
    employee_count: str,
    est_revenue: str,
    total_funding: str,
    hq: str,
) -> str:
    posts_trimmed = [_trim_post(p) for p in (matching_posts or [])[:MAX_POSTS_IN_PROMPT]]
    payload = {
        "lead": {
            "name": name,
            "company": company,
            "position": position,
        },
        "company_facts": {
            "description": company_description,
            "employee_count": employee_count,
            "est_revenue": est_revenue,
            "total_funding": total_funding,
            "hq": hq,
            "competitors": competitors or [],
        },
        "small_talk": small_talk or "",
        "matching_posts": posts_trimmed,
    }
    return (
        f"## ICP / Project Context\n{icp_context.strip() or '(none provided)'}\n\n"
        f"## Lead data\n```json\n{json.dumps(payload, indent=2)}\n```\n\n"
        f"Produce the JSON now. Max {MAX_HOOKS} bullets. Return empty if nothing substantive."
    )


def _normalize_hooks(raw: Any) -> str:
    # The model occasionally returns hooks as a list instead of a newline string.
    if isinstance(raw, (list, tuple)):
        raw = "\n".join(str(item) for item in raw)
    raw = str(raw or "")
    if not raw.strip():
        return ""
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    cleaned: List[str] = []
    for ln in lines:
        if not ln.startswith("- "):
            ln = "- " + ln.lstrip("-•* ").strip()
        cleaned.append(ln)
        if len(cleaned) >= MAX_HOOKS:
            break
    return "\n".join(cleaned)


def generate_hooks(
    name: str,
    company: str,
    position: str = "",
    matching_posts: Optional[List[Dict[str, Any]]] = None,
    small_talk: str = "",
    icp_context: str = "",
    competitors: Optional[List[str]] = None,
    company_description: str = "",
    employee_count: str = "",
    est_revenue: str = "",
    total_funding: str = "",
    hq: str = "",
) -> Dict[str, Any]:
    """Return {"hooks": str, "errors": [str]} — bullets ready for the sheet cell."""
    has_small_talk = bool((small_talk or "").strip())
    has_posts = bool(matching_posts)
    if not has_small_talk and not has_posts and not (competitors or []):
        return {"hooks": "", "errors": ["no usable signal: no small_talk, no posts, no competitors"]}

    user_msg = _build_user_payload(
        name=name, company=company, position=position,
        matching_posts=matching_posts or [],
        small_talk=small_talk,
        icp_context=icp_context,
        competitors=competitors,
        company_description=company_description,
        employee_count=employee_count,
        est_revenue=est_revenue,
        total_funding=total_funding,
        hq=hq,
    )

    text = ""
    for attempt in range(3):
        try:
            resp = _client.messages.create(
                model=CLAUDE_MODEL,
                temperature=0,
                max_tokens=800,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
            )
            text = "".join(block.text for block in resp.content if hasattr(block, "text"))
            break
        except _RETRYABLE as e:
            if attempt == 2:
                return {"hooks": "", "errors": [f"llm_call_failed: {e}"]}
            time.sleep(2 * (attempt + 1))  # 2s, 4s — transient rate-limit/overload
        except Exception as e:
            return {"hooks": "", "errors": [f"llm_call_failed: {e}"]}

    parsed = extract_json(text)
    if parsed is None:
        return {"hooks": "", "errors": [f"unparseable model output: {text[:200]}"]}

    hooks = _normalize_hooks(parsed.get("hooks", ""))
    errors = parsed.get("errors", []) or []
    return {"hooks": hooks, "errors": errors}


if __name__ == "__main__":
    import sys
    payload = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {
        "name": "Tyler Saltsman",
        "company": "ErgoAI",
        "position": "Founder & CEO",
        "small_talk": "- Combat sports — Penn State wrestler + cage fighter\n- Army officer who served in Eastern Europe",
        "matching_posts": [],
        "icp_context": "We sell an AI infrastructure platform for defense + frontier-tech startups.",
    }
    out = generate_hooks(**payload)
    print(json.dumps(out, indent=2))
