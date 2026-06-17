# linkedin_copy_writer

Writes short, human LinkedIn messages that start conversations. Reads like a text from someone who did their homework — not a recruiter, not a sales rep, not an AI.

LinkedIn is where the prospect's attention actually lives — not the inbox they ignore or the phone they dodge — and your profile and identity are attached, so the message already looks more genuine than an email. The goal is to **start a relevant conversation, not close a deal.**

---

## how it works

Three-call pipeline (same shape as `email_copy_writer`):

**Call 1 — signal extraction**
Scans everything known about the lead (posts, small talk, hooks, company data — which may come from LinkedIn, web search, Reddit, HN, X, company sites, podcasts, interviews, blogs, funding/hiring pages) and surfaces the 1-2 strongest signals genuinely relevant to what you sell. Job change, promotion, new role, funding, hiring, product launch, milestone, a post/comment on a topic you touch, podcast/interview, open-source work, a personal interest or mutual connection. Relates data together but never forces a connection — if nothing genuine exists, it says so.

**Call 2 — message drafting + self-review**
Writes a short DM around the signal. The model commits its self-review answers directly into the JSON `review` field — this makes the check auditable. The **three-question test**:
1. `mass_sent_feel` — does this look mass-sent to 500 people? ideal: `"no"`
2. `would_hook_reply` — would this hook a reply? ideal: `"yes"`
3. `reads_human` — does it read like a human wrote it? ideal: `"yes"`

The gate reads the verdict (`no`/`yes`) even when the model appends a justification, so a `review` value like `"no — quotes his own post back to him"` is both auditable and counts as a pass.

**Call 3 — auto-repair (conditional)**
If any check fails, the draft is sent back once with the specific violations and rewritten. Capped at one retry. Anything that survives is logged as `unresolved: ...` in `errors`.

---

## output example

First touch:

```
Hi Andrei, saw your post about doing this by hand — 'brutal' is exactly the right
word. You've got 2,187 engagements in the last month alone. That's a lot of manual
scrolling to find the 40 people who actually matter. Curious what your current
process looks like when you try to pull ICP out of that?
```

Follow-up (when `previous_messages` is passed):

```
Hi Andrei, no worries if the timing's off. Was chatting with another outbound-focused
founder last week and he mentioned the biggest drop-off in his process was the gap
between someone engaging with his content and actually getting them into a sequence —
by the time he'd manually ID'd them, the moment had passed. Curious if that's
something you run into too?
```

---

## message rules baked in

- **Read like a text to a friend.** Short. People skim and have less patience than in their inbox.
- **Break the pattern.** Prospects are buried in recruiter / "can we connect" / someone-selling-something messages. Look unique.
- **Make them feel recognized** — show you understand their goals and problems, and hint you have more if they respond.
- **Not transactional.** No promises, no product features. Talk about the person, their company, and their pain — then twist the knife.
- **Specific, not vague.** Mention something niche so they know you did your homework. No industry jargon.
- **Human texture** — casual shorthand (`thru`), the odd minor error, varied sentence length so it flows.
- **No subject line, no sign-off block, no PS** — it's a DM, your identity is attached.
- **Follow-ups must be useful**, never a bare "just checking in" — add a new angle, a resource, an honest confession, a peer insight, or a no-pressure exit.

---

## inputs

| param | type | required | description |
|---|---|---|---|
| `name` | str | yes | Lead full name |
| `company` | str | yes | Company name |
| `position` | str | no | Job title |
| `buyer_persona` | str | no | Decision Maker / Champion / Non Decision Maker |
| `priority` | str | no | P0 / P1 / P2 |
| `competitors` | str \| list | no | Comma-separated string or list of names |
| `matching_posts` | list | no | `[{url, text, posted_at}]` from post scraper |
| `small_talk` | str | no | Bullet points from small talk scraper |
| `personalisation_hook` | str | no | Bullet points from personalisation_hook skill |
| `icp_context` | str | no | Full stripped context.md — sender's product, audience, value prop |
| `employee_count` | str | no | |
| `est_revenue` | str | no | |
| `total_funding` | str | no | |
| `hq` | str | no | HQ city |
| `previous_messages` | str | no | Prior thread as text — when present, writes a **follow-up** instead of a first touch |

---

## output

```python
{
    "copy": "Hi Andrei, ...",
    "signal_used": "one-line summary of the main signal used",
    "review": {
        "mass_sent_feel": "no",     # verdict "no"  = passed
        "would_hook_reply": "yes",  # verdict "yes" = passed
        "reads_human": "yes"        # verdict "yes" = passed
    },
    "errors": []  # non-empty if signal weak, review failed, or repair didn't fully resolve
}
```

`errors` is informational — a message is always produced. Prefixes:
- `signal_strength=weak/none` — less personalised, consider enriching
- `review_fail: ...` — original draft failed a check (repair was attempted)
- `unresolved: ...` — violation survived the repair pass

---

## calling it

```python
from skills.linkedin_copy_writer import skill as copy_skill

result = copy_skill.write_copy(
    name="Andrei Petrov",
    company="Jungler",
    position="Founder",
    matching_posts=[{"text": "Still doing this by hand and it's brutal...", "posted_at": "2026-05-30"}],
    personalisation_hook="- LI posts got 2,187 engagements last month",
    icp_context="We capture and enrich the profiles who engage with your posts. Sender: Maya, Founder at LeadLoop.",
    competitors=["Clay", "Apollo"],
)
print(result["copy"])
print(result["review"])   # auditable self-check answers

# Follow-up: pass the prior thread
followup = copy_skill.write_copy(
    name="Andrei Petrov", company="Jungler",
    icp_context="...",
    previous_messages="Me: Hi Andrei, your post hit home...\n(no reply after 6 days)",
)
```

Called from `workflows/linkedin_outreach` Step 9 via `write_linkedin_copy(...)`, which forwards these fields and reads `result["copy"]`.

---

## run directly

```bash
# default test case (Andrei + LinkedIn engagement signal)
python3 -m skills.linkedin_copy_writer.skill

# custom payload
python3 -m skills.linkedin_copy_writer.skill '{"name": "Jane Smith", "company": "Acme", "icp_context": "..."}'
```
