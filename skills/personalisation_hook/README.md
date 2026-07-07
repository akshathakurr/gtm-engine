# Personalisation Hook Skill

Given everything the workflow knows about a lead, surface **2-3 one-line
talking points** an SDR can hang a personalised message on.

This skill does **not** write outreach copy. It only surfaces angles. The
copy-writer skill (separate) turns those angles into actual messages.

## What goes into the cell

A short bullet list — at most 3 lines, each one a specific angle:

```
- Just posted about going to AWS re:Invent — open with a re:Invent reference.
- Mentions yoga repeatedly on Twitter — natural opener around mindfulness/recovery.
- Posted a Senior AE job last week — angle: doubling down on sales (we sell sales tooling).
```

If nothing substantive exists, returns empty rather than a generic hook.

## Inputs (called by both outreach workflows)

| Field | Weight | Source |
|---|---|---|
| `small_talk` | **highest** | Step 7 of the workflow (Small Talk Scraper) |
| `matching_posts` | **highest** | Step 6/8 — ICP-relevant LinkedIn posts |
| `competitors` | secondary | Step 5 / Step 1 enrichment |
| `company_description`, `employee_count`, `est_revenue`, `total_funding`, `hq` | tertiary | Enrichment |
| `position`, `name`, `company` | context | Lead row |
| `icp_context` | always loaded | `context/context.md` |

## Output

```python
{
  "hooks": "- <one-line angle>\n- <one-line angle>\n- <one-line angle>",
  "errors": []
}
```

Empty + error if no usable signal.

## Design rules

1. Most weight to `small_talk` + `matching_posts`.
2. Firmographic signals only used when they suggest a *concrete* angle tied to what we sell.
3. Never invent facts. Never paraphrase quotes in a way that changes meaning.
4. Never write outreach copy ("Hi <name>...") — only the angle.
5. 2-3 bullets max. Return empty if nothing substantive exists.

## Cost

One Sonnet 4.6 call per lead. ~$0.005-0.01 per call.

Calls run at temperature 0 (same lead → same hooks) and retry twice (2s/4s
backoff) on transient rate-limit/overload errors before giving up.

## Usage

From a workflow:

```python
from skills.personalisation_hook import skill as hook_skill

result = hook_skill.generate_hooks(
    name=lead["name"],
    company=lead["company"],
    position=lead["position"],
    matching_posts=post_data_by_lead.get(i, []),
    small_talk=small_talk_by_lead.get(i, ""),
    icp_context=icp_context,
    competitors=competitors_by_lead.get(i, []),
    company_description=lead.get("company_description", ""),
    employee_count=lead.get("employee_count", ""),
    est_revenue=lead.get("est_revenue", ""),
    total_funding=lead.get("total_funding", ""),
    hq=lead.get("hq", ""),
)
sheet_cell_value = result["hooks"]
```

Standalone test:

```bash
python -m skills.personalisation_hook.skill '{"name":"Tyler", "company":"ErgoAI", "small_talk":"- Penn State wrestler + cage fighter", "icp_context":"AI infra for defense"}'
```
