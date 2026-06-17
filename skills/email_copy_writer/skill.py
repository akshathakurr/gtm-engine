"""
Email Copy Writer skill.

Three-call pipeline:
  1. Signal extraction — scan all available data and surface the 1-2 strongest,
     genuinely relevant signals (job change, hiring, recent post, promotion, etc.)
  2. Email drafting — write a short, human, mobile-first cold email built around
     those signals. Model commits self-review answers into the JSON `review` field
     (mass_sent_feel, would_hook_reply, reads_human, banned_phrases_used).
  3. Auto-repair (conditional) — if the review flags any violations, the draft is
     sent back once with the specific issues and rewritten. Capped at one retry.

Workflow contract (called from email_outreach Step 10):

    from skills.email_copy_writer import skill as copy_skill
    result = copy_skill.write_copy(
        name="Tyler Saltsman",
        company="ErgoAI",
        position="Founder & CEO",
        email="tyler@ergoai.com",
        buyer_persona="Decision Maker",
        priority="P0",
        matching_posts=[{"url": ..., "text": ..., "posted_at": ...}],
        small_talk="- Combat sports fan\\n- Army veteran",
        personalisation_hook="- Just posted about hiring AI engineers\\n- F1 fan",
        icp_context="<full stripped context.md>",
        employee_count="12", est_revenue="Not available",
        total_funding="2M", hq="Austin", competitors="Acme, Globex",
    )
    # result == {
    #   "copy": "Subject: ...\\n\\nHi Tyler,\\n\\n...",
    #   "signal_used": "...",
    #   "review": {"mass_sent_feel": "no", "would_hook_reply": "yes", "reads_human": "yes", "banned_phrases_used": []},
    #   "errors": []
    # }
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from anthropic import Anthropic

from config import ANTHROPIC_API_KEY, CLAUDE_MODEL

_client = Anthropic(api_key=ANTHROPIC_API_KEY)

MAX_POSTS_IN_PROMPT = 5
MAX_POST_CHARS = 500


# ---------------------------------------------------------------------------
# Call 1 — Signal extraction
# ---------------------------------------------------------------------------

_SIGNAL_SYSTEM = """You analyze research data about a sales lead and identify the strongest, most specific signals that are actually relevant to the sender's business.

A signal is a concrete, recent fact that shows:
- Timing / urgency: job change, new role, promotion, just raised funding, hiring for a role relevant to the sender, just launched something
- Topic alignment: posted or commented about a problem the sender solves, mentioned a competitor, expressed frustration about something the sender can fix
- Personal opener: a specific hobby, shared interest, or recent achievement that creates genuine common ground

RULES:
- Only surface signals you can trace DIRECTLY to the data provided. Never invent or infer facts not present.
- Rank by relevance to the sender's context first. A signal that directly relates to what they sell beats a personal hobby.
- If a signal is only tangentially related, skip it. Forced connections are worse than none.
- Max 2 signals. One strong signal beats two weak ones.
- If no genuine signal exists, return an empty list and say so — a clean company-level email is better than a fabricated hook.

OUTPUT (JSON only):
{
  "signals": [
    {
      "type": "job_change | new_role | promotion | hiring | recent_post | recent_comment | company_milestone | personal_interest | other",
      "detail": "the specific fact from the data, verbatim or paraphrased closely",
      "relevance": "one line: why this is relevant to the sender's product/context"
    }
  ],
  "strength": "strong | moderate | weak | none",
  "notes": "optional — what additional data would make this stronger"
}"""


def _extract_signals(lead_data_block: str, icp_context: str) -> Dict[str, Any]:
    user_msg = (
        f"## Sender Context\n{icp_context.strip() or '(none)'}\n\n"
        f"## All Lead Data\n{lead_data_block}\n\n"
        "Identify the 1-2 strongest signals. Return only the JSON object."
    )
    resp = _client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=500,
        system=_SIGNAL_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
    )
    text = "".join(b.text for b in resp.content if hasattr(b, "text"))
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return {"signals": [], "strength": "none", "notes": ""}
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return {"signals": [], "strength": "none", "notes": ""}


# ---------------------------------------------------------------------------
# Call 2 — Email drafting with self-review loop
# ---------------------------------------------------------------------------

_EMAIL_SYSTEM = """You write cold emails that actually get replies. Not corporate, not AI-sounding, not salesy. Like a message from a sharp founder who did their homework on this specific person.

STRUCTURE (follow this every time):
Subject: {recognizable detail about them or their company}

Hi {first_name},

{Personalized opener referencing a specific signal about their company or situation}

{Their current/inferred process or pain point — be specific, twist the knife gently}

{One-line solution with a risk reversal — what you do, why it's different, de-risk it}

{Soft CTA — specific, low-friction, reference someone on their team if you can}

{sender first name}
{sender title}, {sender company}

PS: {humor-driven opt-out line}

---

WRITING RULES:

LENGTH: 5-8 sentences total in the body. That's it. No exceptions.

MOBILE-FIRST: Never write more than 2 sentences in the same paragraph. People skim on phones. One idea per paragraph. White space is your friend.

SUBJECT LINE:
- Under 8 words
- A recognizable detail — the hook topic, a company fact, their hiring, their tool stack
- No spam: "Quick question", "Following up", "Checking in", "Synergy", "Hope this finds you"
- No caps, no emoji, no exclamation marks
- Good: "your seven-provider phone waterfall", "the AI hiring push", "scaling AEs at Dimmo"

OPENER:
- Never: "I came across your profile", "hope this finds you well", "just wanted to reach out", "I noticed"
- Reference something SPECIFIC — a post, a tool they use, a recent milestone, a hire, their growth
- Make them think "how did they know that"

PAIN POINT:
- Talk about THEIR problem, not your product's features
- The product solves a problem — talk about the problem first, then twist the knife
- Specific > generic. "Managing seven providers with annual contracts" beats "inefficient workflows"

SOLUTION LINE:
- At most one line. "We do X" is enough. Not a feature dump.
- Include a risk reversal when you have one: "or your money back", "no commitment", "free to try"

CTA:
- One ask, low friction: "cool if I send a quick loom?", "worth a 15-min call?", "can I send you my thoughts on how to fix this?", "lmk if that would be of interest"
- Bonus: reference a specific person on their team ("worth passing to whoever owns your data stack?")

PS LINE:
- Humor-driven opt-out. Keeps it human.
- Examples: "If cold emails make you want to throw your laptop, just reply 'no' and I'm out forever.", "P.S. If this is terrible, blame my co-founder — it was their idea."

TONE:
- Casual. Like how you actually talk. Bit of gen-z energy is fine.
- No corporate words: "leverage", "synergy", "streamline", "value-add", "robust", "cutting-edge", "game-changer"
- No AI giveaways: "I hope this message finds you well", "I wanted to reach out", "I came across", "I trust this email finds you", "As someone who"
- Grammar can be slightly loose — sounds more human. "cool if I..." is fine.

WHEN NO SIGNAL EXISTS:
- Don't fake personalization. Open with a sharp observation about their company type, stage, or what they're building.
- Still follow all structure rules.
- Note the signal gap in errors.

---

SELF-REVIEW (do this before outputting):
Draft the email, then honestly answer these 3 questions. Your answers go into the `review` field — they are audited. Do not claim "no" if the email is generic. Do not claim "yes" if the hook is weak.

1. mass_sent_feel — does this look like something mass-sent to 500 people? Answer must be "no".
2. would_hook_reply — would this hook someone's dopamine to respond? Answer must be "yes".
3. reads_human — does this read like an actual human wrote it? Answer must be "yes".
4. banned_phrases_used — list any of these if present in the copy: "I noticed", "I came across your profile", "hope this finds you well", "just wanted to reach out", "Quick question", "Following up", "Checking in", "Hope this finds you". Must be empty [].

If any answer is wrong, rewrite the email before outputting.

---

OUTPUT FORMAT (JSON only, after your self-review):
{
  "copy": "Subject: <line>\\n\\nHi <first_name>,\\n\\n<para1>\\n\\n<para2>\\n\\n<para3>\\n\\n<para4 if needed>\\n\\n<sign-off>\\n\\nPS: <opt-out line>",
  "signal_used": "one-line summary of the main signal you led with, or 'none'",
  "review": {
    "mass_sent_feel": "no",
    "would_hook_reply": "yes",
    "reads_human": "yes",
    "banned_phrases_used": []
  },
  "errors": []
}"""


def _build_lead_data_block(
    name: str,
    company: str,
    position: str,
    email: str,
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
) -> str:
    parts = [
        f"Name: {name}",
        f"Company: {company}",
        f"Position: {position or '(unknown)'}",
        f"Email: {email or '(unknown)'}",
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


def _first_name(name: str) -> str:
    return (name or "").strip().split()[0] if (name or "").strip() else ""


def _build_email_prompt(
    name: str,
    company: str,
    lead_data_block: str,
    signals: List[Dict[str, Any]],
    signal_strength: str,
    icp_context: str,
) -> str:
    if signals:
        signals_text = "\n".join(
            f"- [{s['type']}] {s['detail']} → {s['relevance']}"
            for s in signals
        )
    else:
        signals_text = "(no strong signal found — write a sharp company-level email)"

    return (
        f"## Sender Context\n{icp_context.strip() or '(none provided)'}\n\n"
        f"## Lead Data\n{lead_data_block}\n\n"
        f"## Signals to lead with (strength: {signal_strength})\n{signals_text}\n\n"
        "Now write the email. Do your self-review. Return only the final JSON."
    )


# ---------------------------------------------------------------------------
# Self-review audit + repair
# ---------------------------------------------------------------------------

def _audit_copy(parsed: Dict[str, Any]) -> List[str]:
    """Extract violations from the model's self-review. Returns list of issue strings."""
    issues = []
    review = parsed.get("review") or {}
    if review.get("mass_sent_feel") != "no":
        issues.append("feels mass-sent")
    if review.get("would_hook_reply") != "yes":
        issues.append("weak hook")
    if review.get("reads_human") != "yes":
        issues.append("not human")
    banned = review.get("banned_phrases_used") or []
    if banned:
        issues.append(f"banned phrases used: {', '.join(str(p) for p in banned)}")
    return issues


def _repair_copy(email_prompt: str, draft: str, violations: List[str]) -> Dict[str, Any]:
    """One repair call. Sends draft + violations back and asks for a fix."""
    violation_list = "\n".join(f"- {v}" for v in violations)
    repair_msg = (
        f"{email_prompt}\n\n"
        "---\n\n"
        f"FIRST DRAFT (needs revision):\n{draft}\n\n"
        f"SELF-REVIEW VIOLATIONS:\n{violation_list}\n\n"
        "Fix each violation. Return the corrected email in the same JSON format."
    )
    try:
        resp = _client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1200,
            system=_EMAIL_SYSTEM,
            messages=[{"role": "user", "content": repair_msg}],
        )
        text = "".join(b.text for b in resp.content if hasattr(b, "text"))
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return {"copy": draft, "review": {}, "signal_used": "", "errors": []}
        return json.loads(match.group(0))
    except Exception as e:
        return {"copy": draft, "review": {}, "signal_used": "", "errors": [f"repair_failed: {e}"]}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def write_copy(
    name: str,
    company: str,
    position: str = "",
    email: str = "",
    buyer_persona: str = "",
    priority: str = "",
    matching_posts: Optional[List[Dict[str, Any]]] = None,
    small_talk: str = "",
    personalisation_hook: str = "",
    icp_context: str = "",
    employee_count: str = "",
    est_revenue: str = "",
    total_funding: str = "",
    hq: str = "",
    competitors: str = "",
) -> Dict[str, Any]:
    """Return {"copy": str, "signal_used": str, "errors": [str]}."""
    if not name or not company:
        return {"copy": "", "signal_used": "", "errors": ["missing required fields: name and company"]}

    lead_data_block = _build_lead_data_block(
        name=name, company=company, position=position, email=email,
        buyer_persona=buyer_persona, priority=priority,
        matching_posts=matching_posts or [],
        small_talk=small_talk,
        personalisation_hook=personalisation_hook,
        employee_count=employee_count,
        est_revenue=est_revenue,
        total_funding=total_funding,
        hq=hq,
        competitors=competitors,
    )

    # Call 1: extract signals
    try:
        signal_result = _extract_signals(lead_data_block, icp_context)
    except Exception as e:
        signal_result = {"signals": [], "strength": "none", "notes": str(e)}

    signals       = signal_result.get("signals") or []
    signal_strength = signal_result.get("strength", "none")

    # Call 2: write email with self-review
    email_prompt = _build_email_prompt(
        name=name, company=company,
        lead_data_block=lead_data_block,
        signals=signals,
        signal_strength=signal_strength,
        icp_context=icp_context,
    )

    try:
        resp = _client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1200,
            system=_EMAIL_SYSTEM,
            messages=[{"role": "user", "content": email_prompt}],
        )
        text = "".join(b.text for b in resp.content if hasattr(b, "text"))
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise ValueError(f"No JSON in output: {text[:200]}")
        parsed = json.loads(match.group(0))
    except Exception as e:
        return {"copy": "", "signal_used": "", "errors": [f"llm_call_failed: {e}"]}

    errors = list(parsed.get("errors") or [])
    if signal_strength in ("weak", "none"):
        errors.append(f"signal_strength={signal_strength} — email may be less personalised")

    # Gate on self-review; repair once if violations exist
    copy = (parsed.get("copy") or "").strip()
    audit = _audit_copy(parsed)
    if audit:
        errors.extend(f"review_fail: {v}" for v in audit)
        parsed = _repair_copy(email_prompt, copy, audit)
        copy = (parsed.get("copy") or copy).strip()
        post_audit = _audit_copy(parsed)
        errors.extend(f"unresolved: {v}" for v in post_audit)

    return {
        "copy":         copy,
        "signal_used":  (parsed.get("signal_used") or "").strip(),
        "review":       parsed.get("review") or {},
        "errors":       errors,
    }


# ---------------------------------------------------------------------------
# Run directly for testing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    default_payload = {
        "name": "Finn Carter",
        "company": "Origami",
        "position": "Head of Data",
        "buyer_persona": "Decision Maker",
        "priority": "P0",
        "small_talk": "- Big F1 fan, live-tweeted the Monaco GP\n- Ex-Stripe engineer",
        "personalisation_hook": (
            "- Company uses a seven-provider phone waterfall starting with Bytemine — "
            "each has annual contracts and separate rate limits\n"
            "- Recently posted about data stack frustrations on LinkedIn"
        ),
        "matching_posts": [
            {
                "text": "Managing rate limits across five different data vendors is slowly killing me. "
                        "Each has their own dashboard, their own contract cycle, their own support queue. "
                        "There has to be a better way.",
                "posted_at": "2026-05-28",
                "url": "https://linkedin.com/posts/finn-example",
            }
        ],
        "icp_context": (
            "We sell a phone-number-finding product for data teams. "
            "Higher rate limits than incumbents, no annual contracts, cheaper, better quality. "
            "Money-back guarantee. Sender: Alex, Founder at NumberStack."
        ),
        "employee_count": "45",
        "total_funding": "8M",
        "hq": "San Francisco",
        "competitors": "Bytemine, Apollo, ZoomInfo",
    }

    payload = json.loads(sys.argv[1]) if len(sys.argv) > 1 else default_payload
    out = write_copy(**payload)
    print("\n--- SIGNAL USED ---")
    print(out.get("signal_used", "(none)"))
    print("\n--- EMAIL ---")
    print(out.get("copy", "(empty)"))
    if out.get("errors"):
        print("\n--- ERRORS ---")
        print("\n".join(out["errors"]))
