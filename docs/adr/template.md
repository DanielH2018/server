---
id: "NNNN"
title: Short present-tense statement of the decision
status: Accepted
date: YYYY-MM-DD
governs: []
---

# ADR-NNNN: Short present-tense statement of the decision

## Status

Accepted.

## Context

What forced a decision. The constraints that were real at the time, the options that
existed, and what was not known. Write this so someone who arrives two years later
understands why the obvious choice was not taken.

## Decision

What was decided, in the present tense. One paragraph.

## Consequences

What this costs, what it rules out, and what breaks if it is reversed. An ADR whose
consequences section is empty is a decision nobody stress-tested.

Prefer a consequence that was actually paid over one that was predicted. The project
memory under `~/.claude/projects/-home-ubuntu-server/memory/` records what these decisions
cost in practice, and a specific cost is worth more here than a general caution.

## Governs

Where this decision is enforced in the tree. Each entry is a `file:line` anchor whose line
carries a `# DECIDED:` marker referencing this record. The `governs:` frontmatter list must
match, and `ansible/tests/test_adr_links.py` checks both directions.

**The marker keeps `# DECIDED:` literal and puts the reference after it**, like this:

```
# DECIDED: 8 chars, not 12 — minimum-not-width, and the assert fires before the
# scale-down. (ADR-0011)
```

Not `# DECIDED (ADR-0011):`. `.claude/skills/homelab-review/SKILL.md:89` tells a reviewer to
`grep -rn '# DECIDED:'` before flagging anything in a role, and that grep is literal — moving
the colon drops every annotated marker out of the reviewer's brief, which is the one place
these markers have to appear.

**The `governs:` list may be empty.** Some decisions have no single line that enforces them —
the choice of MkDocs over a flat index is one. An empty list is valid; a wrong one is not.
