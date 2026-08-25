---
id: "0015"
title: A hand-drawn diagram is D2 source in the repo, not Mermaid in the page
status: Accepted
date: 2026-08-24
governs: []
---

# ADR-0015: A hand-drawn diagram is D2 source in the repo, not Mermaid in the page

## Status

Accepted as the choice of tool. The render pipeline it implies is not built — see
*Consequences*.

## Context

The docs site carries three classes of diagram, and they differ in who authors them:

| Class | Source | Rendered by |
|---|---|---|
| Content comes from the tree | the tree itself | a generator emits SVG (`scripts/build_docs.py`) |
| Someone draws it | a checked-in source file | the subject of this record |
| Neither | a committed SVG | an escape hatch |

Only the middle class needed a decision. The obvious candidate was Mermaid: MkDocs Material
renders it from a fenced block, so it costs no binary, no hook and no CI step.

Mermaid was measured against the diagram the site most needs — the backup chain, which has
nested containers, two storage tiers and edges that cross between them. It routes those edges
badly enough that the picture asserts relationships the system does not have. A diagram that
is merely ugly is a cost; one that misleads is worse than no diagram, because a reader
believes it.

## Decision

A diagram somebody draws is D2 source committed at `docs/diagrams/*.d2`, rendered to a
committed SVG under `docs/assets/generated/`. Mermaid is not used on this site.

The rendered SVG is committed alongside its source rather than built at page-render time, so
the site stays a static tree that any host can serve.

## Consequences

**D2 is a second binary**, needing a pinned version on the build host and in CI — the cost
Mermaid would not have carried. That cost is why nothing was built: the choice was made and
the pipeline it implies was the last, dependency-free slice of the docs-UI programme, and it
was never started.

**Committing a rendered artefact makes staleness the failure mode.** A changed `.d2` whose
`.svg` was not re-rendered shows the previous picture while the diff shows the new one, so a
reviewer sees the change and believes it shipped. A `--check` hook that renders to a temp
directory and compares is what catches it. The hook must not re-render in place: a hook that
writes the tree it is checking makes `git diff` show changes the author did not make.

**Nothing yet stops the next person reaching for Mermaid**, because the rule lives in this
record rather than in a gate. Until the pipeline exists, this ADR is the only thing that says
no.

The unexecuted steps are kept at
[`archive/diagrams-plan.md`](../archive/diagrams-plan.md).

## Governs

No single line. `governs:` is empty: no code enforces this yet, which is itself recorded
above.
