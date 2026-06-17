# project_context

Turns the raw files under `/context` into one clean context string for LLM prompts.

This is the one skill that **reads** the context files — every other skill consumes data the workflow already gathered, but `project_context`'s whole job is to *be* the context provider.

---

## why it exists

The fallback some workflows use is "concatenate every `.md` in `/context`." That's noisy:

- `context/context.md` is a **questionnaire** — each `## Section` wraps the real answer in `### Answer`, surrounded by `<!-- Question / Used by / Example -->` scaffolding and `(fill this in)` placeholders. Dumping it raw feeds the model instructions-to-itself instead of facts.
- Empty template sections (a blank `competitors.md` that's just `- **Name:**` / `- **Website:**` labels with no values) add bulk and zero signal.

`get_context()` fixes both:

1. **Strips scaffolding** — removes `<!-- -->` comment blocks.
2. **Extracts answers** — for questionnaire files, keeps only the `### Answer` body; for free-form files (`icp.md`, `competitors.md`) keeps each `## Section` as written.
3. **Drops empties** — skips `(fill this in)` / `(none)` / `(skip)` placeholders and sections that are only empty labels.
4. **Labels what's kept** — each surviving section is emitted under its `## Header`.

---

## interface

```python
from skills.project_context import skill as project_context_skill

ctx = project_context_skill.get_context()   # -> str
```

| param | type | default | description |
|---|---|---|---|
| `context_dir` | str | `config.CONTEXT_DIR` | folder to read `.md` files from (point at a per-project subfolder if you have one) |

**Returns** a clean, labeled context string. Returns `""` — never raises — when the directory is missing or holds no usable content, so callers degrade gracefully.

---

## output shape

```
## Product
<the answer>

## Ideal Customer Profile
<the answer>

---

## Competitor 1
- **Name:** Acme
- **What they do:** ...
```

Files are joined with `\n\n---\n\n`; sections within a file with `\n\n`.

---

## used by

- `workflows/linkedin_comment_helper` — `get_context()` provides the project context that each candidate LinkedIn post is scored against. The workflow gates the import with a `_PROJECT_CONTEXT_AVAILABLE` flag and falls back to a raw concat of `/context` if the skill is missing.

Any workflow that needs clean project context should call this rather than rolling its own loader.

---

## run directly

```bash
# assemble from the default context/ dir
python3 -m skills.project_context.skill

# point at a specific folder (e.g. a per-project context subfolder)
python3 -m skills.project_context.skill context/acme
```
