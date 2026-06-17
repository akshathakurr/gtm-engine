# Skills

Skills are reusable prompt modules that workflows call when they need an LLM to do something narrow — surface personalisation angles, write outreach copy, classify a post, etc.

Each skill lives in its own folder: `skills/<name>/skill.py` + `__init__.py` + `README.md`.

Workflows import them like this:

```python
from skills.email_copy_writer import skill as copy_skill
result = copy_skill.write_copy(...)
```

---

## current skills

| skill | what it does | status |
|---|---|---|
| `personalisation_hook` | Given lead data, surfaces 2-3 one-line talking points (angles, not copy) | built |
| `email_copy_writer` | Writes a complete cold email (subject + body + PS) around the strongest signal | built |
| `linkedin_copy_writer` | Writes a short, human LinkedIn DM (or follow-up) around the strongest signal | built |

---

## folder structure

```
skills/
  <name>/
    __init__.py       # exports `skill`
    skill.py          # all logic lives here
    README.md         # inputs, output, usage
```

The `skill.md.example` file at the root shows an older single-file pattern — **ignore it, the folder pattern above is correct**.

---

## adding a new skill

1. Create `skills/<name>/` with the three files above.
2. `__init__.py` should just be `from . import skill` / `__all__ = ["skill"]`.
3. `skill.py` exports one main function (e.g. `write_copy`, `generate_hooks`).
4. Skills consume data — they do not scrape. All inputs come from the workflow.
5. Follow the empty-over-padding rule: weak input → return empty + error, never generic output.

---

## design rules

- Skills are NOT scrapers. They receive data already gathered by the workflow — they don't read files or fetch data themselves. (Workflows read the `/context` files directly and pass the relevant context in as a parameter, e.g. `icp_context`.)
- One main public function per skill. Keep the import surface small.
- Return dicts with at least `errors: []` so callers can always check for problems.
- Stub-skip pattern: if a skill isn't built yet, the workflow gates with a try/import and a `_AVAILABLE` flag so it can still run without it.
