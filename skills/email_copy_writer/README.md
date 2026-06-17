# email_copy_writer

Writes cold emails that get replies. Short, human, mobile-first — built around actual signals about the lead.

---

## how it works

Three-call pipeline:

**Call 1 — signal extraction**
Scans everything known about the lead (posts, small talk, hooks, company data) and identifies the 1-2 strongest signals genuinely relevant to what you sell. Job change, hiring for a relevant role, posted about a pain point you solve, recent funding, personal interest that creates real common ground. If nothing strong exists, it says so — a clean company-level email beats a fabricated hook.

**Call 2 — email drafting + self-review**
Writes the email around the signal. The model commits its self-review answers directly into the JSON `review` field — this makes the check auditable (claiming "no" on a generic email is a failure). Four checks:
1. `mass_sent_feel` — does this look mass-sent? must be `"no"`
2. `would_hook_reply` — would this hook a reply? must be `"yes"`
3. `reads_human` — does it read human? must be `"yes"`
4. `banned_phrases_used` — list of any banned phrases found. must be `[]`

**Call 3 — auto-repair (conditional)**
If any review check fails, the draft is sent back once with the specific violations and rewritten. Capped at one retry. Any violations that survive are logged as `unresolved: ...` in `errors`.

---

## output example

```
Subject: your seven-provider phone waterfall

Hi Finn,

Saw your post about managing rate limits across five vendors — and apparently Origami
has seven providers in your phone waterfall, each with their own annual contract and
limit ceiling. That's not a data stack, that's a hostage situation.

Every time you need more volume, you're negotiating with someone. Every time a
contract renews, you're locked in whether the data's good or not.

We built NumberStack to replace the whole waterfall — one provider, higher rate
limits than Bytemine or Apollo, no annual contract, and a money-back guarantee if
the match quality doesn't hold up.

Worth a 15-min call to see if we can cut two or three of those vendors out? Happy
to send a quick Loom first if that's easier for whoever owns the vendor contracts.

Alex
Founder, NumberStack

PS: If cold emails from data vendors are your version of a safety car period, just
reply 'no' and I'll never contact you again.
```

---

## email rules baked in

- **5-8 sentences** in the body. No exceptions.
- **Mobile-first** — max 2 sentences per paragraph. People skim on phones.
- **Pain points, not features** — talk about their problem, not your product's feature list.
- **Casual tone** — gen-z energy is fine. No "leverage", "synergy", "streamline", "I hope this finds you".
- **Natural feel** — casual shorthand ("thru" for "through"), a minor imperfection or two (without looking sloppy), and varied sentence length so it doesn't read AI-perfect.
- **Subject** — under 8 words, a recognizable detail, no spam triggers (`"Quick question"`, `"Following up"`, `"Checking in"`, `"Synergy"`, `"Hope this finds you"`). `"Re:"` is allowed.
- **CTA** — one ask, low friction. Optional: reference a specific person on their team.
- **PS** — humor-driven opt-out. Always present.

### follow-ups

The prompt also carries follow-up guidance (~42% of replies come from follow-ups, not the first touch):

- Add something every time — a new angle, pain, proof, or resource. Never an empty "just bumping this."
- Never "just checking in" — it measurably reduces meetings booked.
- Read as a natural continuation of an ongoing thread, referencing prior messages — not a brand-new outreach each time.

Note: the skill writes one email per call. There is no `followup_number` param yet, so this guidance fires only when the caller signals (via `icp_context` / lead data) that the email is a follow-up.

---

## inputs

| param | type | required | description |
|---|---|---|---|
| `name` | str | yes | Lead full name |
| `company` | str | yes | Company name |
| `position` | str | no | Job title |
| `email` | str | no | Email address (context only) |
| `buyer_persona` | str | no | Decision Maker / Champion / Non Decision Maker |
| `priority` | str | no | P0 / P1 / P2 |
| `matching_posts` | list | no | `[{url, text, posted_at}]` from post scraper |
| `small_talk` | str | no | Bullet points from small talk scraper |
| `personalisation_hook` | str | no | Bullet points from personalisation_hook skill |
| `icp_context` | str | no | Full stripped context.md — sender's product, audience, value prop |
| `employee_count` | str | no | |
| `est_revenue` | str | no | |
| `total_funding` | str | no | |
| `hq` | str | no | HQ city |
| `competitors` | str | no | Comma-separated |

---

## output

```python
{
    "copy": "Subject: ...\n\nHi <first_name>,\n\n...\n\nPS: ...",
    "signal_used": "one-line summary of the main signal used",
    "review": {
        "mass_sent_feel": "no",       # "no" = passed
        "would_hook_reply": "yes",    # "yes" = passed
        "reads_human": "yes",         # "yes" = passed
        "banned_phrases_used": []     # empty = passed
    },
    "errors": []  # non-empty if signal weak, review failed, or repair didn't fully resolve
}
```

`errors` is informational — a fallback email is always produced. Prefixes:
- `signal_strength=weak/none` — less personalised, consider enriching
- `review_fail: ...` — original draft failed a check (repair was attempted)
- `unresolved: ...` — violation survived the repair pass

---

## calling it

```python
from skills.email_copy_writer import skill as copy_skill

result = copy_skill.write_copy(
    name="Finn Carter",
    company="Origami",
    position="Head of Data",
    matching_posts=[{"text": "Managing rate limits across five vendors...", "posted_at": "2026-05-28"}],
    small_talk="- F1 fan, live-tweeted Monaco GP",
    icp_context="We sell a phone-number-finding product. Sender: Alex, Founder at NumberStack.",
    competitors="Bytemine, Apollo, ZoomInfo",
)
print(result["copy"])
print(result["review"])   # auditable self-check answers
```

---

## run directly

```bash
# default test case (Finn + seven-vendor waterfall)
python3 -m skills.email_copy_writer.skill

# custom payload
python3 -m skills.email_copy_writer.skill '{"name": "Jane Smith", "company": "Acme", "icp_context": "..."}'
```
