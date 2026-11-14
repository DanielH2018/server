# G11 — The binding sweep

Status: PLANNED.

Depends on: [G7](g7-removed-names.md) and [G10](g10-value-rows.md). The sweep reads the
doc set the first defines and binds sentences to the rows the second builds.
[G9](g9-blocking-gate.md) stopped at its measurement on 2026-11-13, so the sweep binds a
description to the advisory notice, not to a gate.

Unblocks: none.

[ADR-0031](../../adr/0031-each-kind-of-fact-has-one-stateless-detector.md) records the
decision this slice serves.

## Goal and exit criterion

G7 and G10 build detectors. This slice gives them something to read. It is a fan-out
over the instruction files: `CLAUDE.md`, rules, skills and agents.

Exit: `fact_status.py report` shows the five root `CLAUDE.md` sections citing content
atoms, and the load-weighted share of sections that cite nothing has fallen in every
batch.

## Baseline at `4d85f67c5`

- 176 of 411 `CLAUDE.md` sections cited nothing, including the five root sections that
  loaded 177 times each in the instruction log's window.
- 136 of 671 citations pinned content. The other 535 were path atoms.

## Design

Each batch classifies every sentence in its sections and moves it to its home:

```diff
+  a value                 → a VALUES row (G10)
+  a behaviour             → a test node with "# fact:"
+  a description of code   → a symbol, YAML or marker citation, which the notice lists
+  history                 → **HISTORY —
   a directive             → stays, cites nothing
 .claude/rules/facts.md   rewritten to the end-state model
 .claude/agents/homelab-docs-freshness-reviewer.md
-  tiers 2 and 3 (churn-ranked guesses)
+  reads the sections `report` ranks as loaded most and cited least
```

Batches run in order of load count, from `report`'s ranking. No ratchet enforces the
count: a ratchet on uncited sections fails restructures and moves. `report` prints the two
shares, and they are the measure.

## Steps, as PRs

1. The root `CLAUDE.md` and `.claude/rules/`, as the pilot batch. Thirty days after it,
   the notice's unedited rows per content citation are compared with the 0.29 that G9
   measured.
2. Roughly 25 batches over the nested `CLAUDE.md` files, skills and agents, through the
   issue fan-out.
3. `.claude/rules/facts.md` and the freshness reviewer's tiers.

## Verification

Both shares move after each batch. A batch that adds citations and moves neither share is
inert.

## Risks and what is not verified

- Prose that cites nothing and names nothing stays undetected. The sweep shrinks it.
- The sweep raises the number of notice rows. G9 measured 0.29 unedited rows per content
  citation in 30 days, and 6% of them left a false sentence.

## Complexity delta

Removes typed facts and two reviewer tiers. Adds citations, tests and rows.
