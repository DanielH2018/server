# G11 — The binding sweep

Status: IN PROGRESS. The measure is corrected
([#8699](https://github.com/DanielH2018/server/pull/8699)), and the pilot batch, step 1,
is done ([#8707](https://github.com/DanielH2018/server/pull/8707)). Its 30-day comparison
is due on 2026-12-14.

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

## Before the pilot: the measure

`fact_status.py report` ranked a section with no prose as citing nothing. On 2026-11-14
five of its ten most-read rows were of that kind: the root `CLAUDE.md` title, and the
frontmatter that opens four rule files. 14 of the 582 sections in the instruction files
hold no prose: 10 preambles and 4 headings whose text all sits under subheadings.
`scripts/lib/facts/citations.py:has_prose` leaves them out, and the backlog fell from 262
rows to 248. The exit criterion reads this ranking, so the correction comes before the
first batch.

## Step 1 result

The pilot bound the four root `CLAUDE.md` sections that cited nothing. The plan counted
five: the fifth was the file's title, which holds no prose.

| Section | Sentence | Bound to |
|---|---|---|
| After a PR Merges | only `land.sh` reports how a landing ended | the `Verdict` symbol |
| When to wait | `probe.py landing` reads the hold and master CI | the `run_landing` symbol |
| When to wait | a docs-only change ends `nothing-to-deploy` | a test node |
| Parallel Claude Sessions | the `worktree-` branch prefix | the `branch_prefix` symbol |
| Parallel Claude Sessions | the `--sessions` listing | the `print_all_sessions` symbol |
| Pre-commit Hooks | the files the commit-time hooks run | the `hook_files` symbol |
| Pre-commit Hooks | the advice `repo_guards.py` prints | the `ADVICE` symbol |
| Pre-commit Hooks | one untracked file parks the checkout sync | a test node |

- **The one value already had a detector, and keeps it.** The section says the hooks run
  "twenty-five" test files, and
  `tests/repo/test_static_ratchet_hook.py::test_claude_md_states_how_many_files_the_hook_runs`
  holds that count against the hooks. The test fails and rewrites nothing. Moving it to a
  `VALUES` row would add the fixer, and is not part of this batch.
- **Three sentences stay unbound.** "CI's `pytest` job runs the whole suite on every PR"
  has no citable owner: the job's YAML holds action pins Renovate bumps, which the
  `renovate-pin` rule refuses. The Claude Code binary's refusal of a command it cannot
  verify, and the `ExitWorktree` warning, describe the harness, which this tree does not
  hold.
- **Four rule sections are left.** Two are directives and cite nothing by design: "A check
  that reaches over a network measures its transport first" in `python-layout.md`, and
  "Never print a secret value" in `k8s-templates.md`. The two in `docker.md` wait for a
  fan-out batch that was editing that file on 2026-11-14.
- The root file grew by eight citations. The 30-day comparison counts the notice rows they
  draw against the 0.29 unedited rows per content citation that G9 measured.

## Verification

Both shares move after each batch. A batch that adds citations and moves neither share is
inert.

## Risks and what is not verified

- Prose that cites nothing and names nothing stays undetected. The sweep shrinks it.
- The sweep raises the number of notice rows. G9 measured 0.29 unedited rows per content
  citation in 30 days, and 6% of them left a false sentence.

## Complexity delta

Removes typed facts and two reviewer tiers. Adds citations, tests and rows.
