"""
LinkedIn Copy Writer skill.

Writes short, human LinkedIn messages that start conversations. LinkedIn is
where the prospect's attention actually lives — not the inbox they ignore or the
phone they dodge — so the message has to read like a text from someone who did
their homework, not a recruiter or a sales rep.

Three-call pipeline (mirrors email_copy_writer):
  1. Signal extraction — scan all available data (LinkedIn, web search, Reddit,
     HN, X, company sites, podcasts, interviews, blogs, funding/hiring pages,
     plus the sender's own context) and surface the 1-2 strongest, genuinely
     relevant signals. Never invent one.
  2. Message drafting — write a short DM built around those signals. Model commits
     its self-review answers into the JSON `review` field (the three-question
     test: mass_sent_feel, would_hook_reply, reads_human).
  3. Auto-repair (conditional) — if the review flags any violation, the draft is
     sent back once with the specific issues and rewritten. Capped at one retry.

Workflow contract (called from linkedin_outreach Step 9):

    from skills.linkedin_copy_writer import skill as copy_skill
    result = copy_skill.write_copy(
        name="Andrei Petrov",
        company="Jungler",
        position="Founder",
        buyer_persona="Decision Maker",
        priority="P0",
        competitors=["Clay", "Apollo"],
        matching_posts=[{"url": ..., "text": ..., "posted_at": ...}],
        small_talk="- Combat sports fan",
        personalisation_hook="- LI posts got 2,187 engagements last month",
        icp_context="<full stripped context.md>",
        employee_count="12", est_revenue="Not available",
        total_funding="2M", hq="Austin",
    )
    # result == {
    #   "copy": "Hi Andrei, ...",
    #   "signal_used": "...",
    #   "review": {"mass_sent_feel": "no", "would_hook_reply": "yes", "reads_human": "yes"},
    #   "errors": []
    # }

Standalone follow-up use: pass `previous_messages` (the prior thread, as text) and
the skill writes a follow-up using the follow-up tactics instead of a first touch.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Union

from config import CLAUDE_MODEL
from skills._copy_core import (
    client,
    extract_json,
    extract_signals,
    build_lead_data_block,
    audit_copy,
    repair_copy,
)


# ---------------------------------------------------------------------------
# Call 1 — Signal extraction
# ---------------------------------------------------------------------------

_SIGNAL_SYSTEM = """You analyze research data about a sales lead and identify the strongest, most specific signals that are actually relevant to the sender's business.

The data can come from anywhere: LinkedIn, web search, Reddit, Hacker News, Twitter/X, company websites, podcasts, interviews, blogs, funding announcements, hiring pages, and the sender's own context.

A signal is a concrete fact that gives you a genuine reason to reach out. Look for:
- Timing: job change, promotion, recently started a role, funding announcement, hiring activity, product launch, company milestone, growth announcement, partnership, team expansion
- Topic alignment: a post or comment related to what the sender does, an industry opinion they voiced, a problem they described, a competitor they mentioned, a podcast appearance or interview, an open-source contribution
- Common ground: a personal interest, a mutual connection, a shared experience

RULES:
- Only surface signals you can trace DIRECTLY to the data provided. Never invent or infer facts not present.
- Relate the data together, but do NOT force connections. Only use a signal when it genuinely makes sense.
- Rank by relevance to the sender's context first. A signal that connects to what the sender does beats a generic personal fact.
- Max 2 signals. One strong signal beats two weak ones.
- Not every prospect has a useful signal. If none genuinely exists, return an empty list and say so.

OUTPUT (JSON only):
{
  "signals": [
    {
      "type": "job_change | promotion | new_role | funding | hiring | product_launch | company_milestone | growth | partnership | team_expansion | post | comment | industry_opinion | podcast | interview | open_source | personal_interest | mutual_connection | shared_experience | other",
      "detail": "the specific fact from the data, verbatim or paraphrased closely",
      "relevance": "one line: why this is a genuine reason for the sender to reach out"
    }
  ],
  "strength": "strong | moderate | weak | none",
  "notes": "optional — what additional data would make this stronger"
}"""


# ---------------------------------------------------------------------------
# Call 2 — Message drafting with self-review loop
# ---------------------------------------------------------------------------

_MESSAGE_SYSTEM = """You write LinkedIn messages that get replies and start conversations. Like a text to a friend — not a recruiter, not a sales rep, not an AI.

Your prospect's attention lives on LinkedIn, not the inbox they ignore or the phone they dodge. Your whole profile and identity are attached to the message, so it already looks more genuine than an email. People scroll fast and have even less patience here than in their inbox, and they're bombarded with hiring/recruiting/"can we connect"/someone-selling-something messages. Break that pattern. Look unique.

WHAT YOU'RE OPTIMIZING FOR:
- Make the person feel recognized.
- Make it clear you understand their goals and their problems.
- Hint that you have more to offer if they respond.
- Start a relevant conversation — NOT close a deal. Write the copy accordingly.

WRITING RULES:
- Write like you're texting a friend. It should read like a text.
- Keep it short. People skim. A few short sentences, no more.
- Show a clear, specific observation and an empathetic tone.
- Be very specific. No vague industry jargon.
- Mention something related but niche so they know you did your homework.
- Vary sentence length so the words flow — mix short, punchy lines with a slightly longer one.
- Use casual shorthand like "thru" instead of "through" to feel less formal.
- Include a minor error or two (without looking sloppy) to break the robotic, AI-perfect feel.

WHAT NOT TO DO:
- Do NOT come across as transactional. Do NOT promise anything. Do NOT focus on the solution you provide.
- Do NOT talk about your product's features. Founders talk too much about what they're building and not enough about how it helps the customer. Talk about the person, their company, and their pain. People care about themselves and their own problems. Your product solves a problem — talk about that problem and twist the knife in it to get them interested.
- Come across as a human who is approachable and genuine.

SHAPE (loose — this is a text, not a template):
- Optional light greeting ("Hi {first_name},").
- A specific observation tied to a real signal about them or their company.
- A curious, low-pressure question or a gentle twist on their problem that invites a reply.
Examples of the vibe:
- "Hi Andrei, your LI posts got 2,187 comments + likes in the past month — are you doing anything to pull your ICP out of everyone who's engaging?"
- "Saw Hammitt's been expanding into premium accessories. Do you guys have a loyalty program in place yet?"
- "Hi Chuck, curious if your Java engineers have found a coding assistant they actually like?"

WHEN NO SIGNAL EXISTS:
- Don't fake it. Lead with a sharp, specific observation about their company, stage, or role.
- Still short, still a text, still a question that invites a reply.
- Note the signal gap in errors.

FOLLOW-UP MESSAGES:
When prior messages in the thread are provided, write a follow-up — not a fresh outreach. Don't just "check in." Be useful. Add something. Pick whichever angle fits:
- Noticed something new: "Saw you guys just launched {thing}. How's that going so far?"
- Relevant resource: "Came across this {article/report} about {their challenge}. Thought of you — want me to send it over?"
- Honest confession: "Realized my last message was vague. Basically I'm curious how you all handle {specific process}?"
- Peer insight: "Was chatting with another {their role} and they mentioned {pain point}. You running into that too?"
- No-worries exit: "Totally get it if the timing's off. If {pain point} ever becomes a priority, happy to share what's working for other {their role}s."
Reference the thread naturally; make it feel like an ongoing conversation.

---

SELF-REVIEW (the three-question test — do this before outputting):
Draft the message, then honestly answer these 3 questions. Your answers go into the `review` field — they are audited. Don't claim "no" if it's generic. Don't claim "yes" if the hook is weak. If any answer isn't the ideal, rewrite the message before outputting.

1. mass_sent_feel — does this look like something you mass-sent to 500 people? Ideal answer: "no".
2. would_hook_reply — would this hook someone's dopamine to respond? Ideal answer: "yes".
3. reads_human — does this read like an actual human wrote it? Ideal answer: "yes".

---

OUTPUT FORMAT (JSON only, after your self-review):
{
  "copy": "the LinkedIn message text, exactly as it should be sent",
  "signal_used": "one-line summary of the main signal you led with, or 'none'",
  "review": {
    "mass_sent_feel": "no",
    "would_hook_reply": "yes",
    "reads_human": "yes"
  },
  "errors": []
}"""


def _join_competitors(competitors: Union[str, List[str], None]) -> str:
    if not competitors:
        return ""
    if isinstance(competitors, str):
        return competitors.strip()
    return ", ".join(str(c).strip() for c in competitors if str(c).strip())


def _build_message_prompt(
    lead_data_block: str,
    signals: List[Dict[str, Any]],
    signal_strength: str,
    icp_context: str,
    previous_messages: str,
) -> str:
    if signals:
        signals_text = "\n".join(
            f"- [{s['type']}] {s['detail']} → {s['relevance']}"
            for s in signals
        )
    else:
        signals_text = "(no strong signal found — lead with a sharp, specific observation)"

    prompt = (
        f"## Sender Context\n{icp_context.strip() or '(none provided)'}\n\n"
        f"## Lead Data\n{lead_data_block}\n\n"
        f"## Signals to lead with (strength: {signal_strength})\n{signals_text}\n\n"
    )

    if previous_messages and previous_messages.strip():
        prompt += (
            f"## Prior thread (write a FOLLOW-UP, not a fresh outreach)\n"
            f"{previous_messages.strip()}\n\n"
        )

    prompt += "Now write the LinkedIn message. Do your three-question self-review. Return only the final JSON."
    return prompt


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def write_copy(
    name: str,
    company: str,
    position: str = "",
    buyer_persona: str = "",
    priority: str = "",
    competitors: Union[str, List[str], None] = None,
    matching_posts: Optional[List[Dict[str, Any]]] = None,
    small_talk: str = "",
    personalisation_hook: str = "",
    icp_context: str = "",
    employee_count: str = "",
    est_revenue: str = "",
    total_funding: str = "",
    hq: str = "",
    previous_messages: str = "",
) -> Dict[str, Any]:
    """Return {"copy": str, "signal_used": str, "review": dict, "errors": [str]}."""
    if not name or not company:
        return {"copy": "", "signal_used": "", "review": {}, "errors": ["missing required fields: name and company"]}

    lead_data_block = build_lead_data_block(
        name=name, company=company, position=position,
        buyer_persona=buyer_persona, priority=priority,
        matching_posts=matching_posts or [],
        small_talk=small_talk,
        personalisation_hook=personalisation_hook,
        employee_count=employee_count,
        est_revenue=est_revenue,
        total_funding=total_funding,
        hq=hq,
        competitors=_join_competitors(competitors),
    )

    # Call 1: extract signals
    try:
        signal_result = extract_signals(_SIGNAL_SYSTEM, lead_data_block, icp_context)
    except Exception as e:
        signal_result = {"signals": [], "strength": "none", "notes": str(e)}

    signals         = signal_result.get("signals") or []
    signal_strength = signal_result.get("strength", "none")

    # Call 2: write message with self-review
    message_prompt = _build_message_prompt(
        lead_data_block=lead_data_block,
        signals=signals,
        signal_strength=signal_strength,
        icp_context=icp_context,
        previous_messages=previous_messages,
    )

    try:
        resp = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=800,
            system=_MESSAGE_SYSTEM,
            messages=[{"role": "user", "content": message_prompt}],
        )
        text = "".join(b.text for b in resp.content if hasattr(b, "text"))
        parsed = extract_json(text)
        if parsed is None:
            raise ValueError(f"No JSON in output: {text[:200]}")
    except Exception as e:
        return {"copy": "", "signal_used": "", "review": {}, "errors": [f"llm_call_failed: {e}"]}

    errors = list(parsed.get("errors") or [])
    if signal_strength in ("weak", "none"):
        errors.append(f"signal_strength={signal_strength} — message may be less personalised")

    # Gate on self-review; repair once if violations exist
    copy = (parsed.get("copy") or "").strip()
    audit = audit_copy(parsed)
    if audit:
        errors.extend(f"review_fail: {v}" for v in audit)
        parsed = repair_copy(_MESSAGE_SYSTEM, message_prompt, copy, audit, max_tokens=800)
        copy = (parsed.get("copy") or copy).strip()
        post_audit = audit_copy(parsed)
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
        "name": "Andrei Petrov",
        "company": "Jungler",
        "position": "Founder",
        "buyer_persona": "Decision Maker",
        "priority": "P0",
        "personalisation_hook": (
            "- LinkedIn posts pulled 2,187 comments + likes in the past month\n"
            "- Posts mostly about outbound sales and finding ICP contacts"
        ),
        "matching_posts": [
            {
                "text": "Outbound is a numbers game until it isn't. The hard part is figuring out "
                        "which of the thousands of people engaging with your content are actually "
                        "your ICP. Still doing this by hand and it's brutal.",
                "posted_at": "2026-05-30",
                "url": "https://linkedin.com/posts/andrei-example",
            }
        ],
        "icp_context": (
            "We sell a tool that captures the profiles who engage with your LinkedIn posts "
            "and enriches them with profile + company data so you can filter your ICP out of "
            "your own audience. Sender: Maya, Founder at LeadLoop."
        ),
        "employee_count": "8",
        "total_funding": "2M",
        "hq": "Austin",
        "competitors": ["Clay", "Apollo"],
    }

    payload = json.loads(sys.argv[1]) if len(sys.argv) > 1 else default_payload
    out = write_copy(**payload)
    print("\n--- SIGNAL USED ---")
    print(out.get("signal_used", "(none)"))
    print("\n--- MESSAGE ---")
    print(out.get("copy", "(empty)"))
    print("\n--- REVIEW ---")
    print(json.dumps(out.get("review", {}), indent=2))
    if out.get("errors"):
        print("\n--- ERRORS ---")
        print("\n".join(out["errors"]))
