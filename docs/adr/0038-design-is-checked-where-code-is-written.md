---
id: "0038"
title: Design is checked where code is written, and a design finding only takes code out
status: Accepted
date: 2026-11-19
governs: []
---

# ADR-0038: Design is checked where code is written, and a design finding only takes code out

## Status

Accepted. ADR-0040 records the gates that followed the same day, from the options this
record's *Consequences* section sets aside.

## Context

Sessions kept finding code to prune after it landed: dead options, second implementations,
pass-through helpers, tests that assert a removal. The question was why the instruction files
did not prevent it.

**What the rework was.** Measured on 2026-11-19 over the 30 days before it: 1,944 commits
landed on `master`, and 485 had a cleanup verb in the subject. A sample of every eighth one
gave 59 commits, of which 34 were rework. 18 of the 34 removed what the Ansible and deployer
migrations had made obsolete. 12 removed code that was the wrong shape when it was written,
and 4 were mixed. The wrong-shape code that could be dated had a median age of 41 days at
removal (11 cases). The classes rest on commit messages and stats, not on full diffs, so the
shares are rough.

**Where design was checked.** Nowhere on the path from a session to `master`. The module
design rules are prose in the operator's user-level `CLAUDE.md`. The fan-out brief said
nothing about design. The fan-out reviewer's categories were `correctness`, `security`,
`test`, `convention` and `other`, and its prompt named no design rule. `pyproject.toml`
selects bug-catching lint rules only, by a stated policy. The one enforced design measure was
a module's length.

**Each standard arrived as a sweep.** In the week before, a vulture sweep, a comment prune, a
prune of tests for removals, and seven parse-at-the-boundary findings (#9258 to #9264) each
fixed the instances that existed. None changed what the next change is told or checked
against.

**Why a reviewer that can add is a risk.** A reviewer told to report everything it finds does
find something, and an implementer that fixes every finding adds layers and tests. One of the
sampled removals was a dormant test written as a review fix. Anthropic's Claude Code
best-practices page describes the same effect.

## Decision

The fan-out brief lists what a change must not add, and the fan-out reviewer reports each
instance as a `design` finding. Both read one list, `scripts/dev/fanout_lib/brief.py:DESIGN_RULES`,
so a finding names a rule in the words the implementer was given. A `design` finding only
takes code out: its fix deletes, merges or inlines something the change added, and the
reviewer is told not to report one whose fix adds a layer, an option or a test.

`scripts/dev/repo_guards.py` prints what a branch adds, net of what it removes, each new
top-level name that nothing outside tests uses, and each new function that takes or returns a
raw dict (`scripts/lib/branch_adds.py:added_advice`).
It is advice and changes no exit code.

## Consequences

The list reaches fan-out batches only. An interactive session gets the `branch adds:` line
from `repo_guards.py` and nothing else from this decision. The share of commits each kind of
session writes was not measured.

The reviewer's restriction is prose in its prompt, and nothing checks that a `design`
finding's fix removes lines. If design findings start to grow diffs, the fix round is where
to measure it.

`branch adds:` counts a name as used when it appears as a whole word in any tracked file
outside tests and docs. A guard library that only tests call is listed every time, and the
session says so in the PR body. The line stays advice for that reason.

This decision does not reach the larger half of the rework: code a migration made obsolete,
left behind because no slice could delete what another slice still called. It also turns no
class into a gate. Both were set aside, with these options, when the decision was made:

- Run vulture as a ratchet over the list `tests/repo/test_vulture_test_only_names.py` freezes.
- Run the `prune-tests` census at commit time.
- Count raw-dict signatures outside named parse modules, and ratchet the count down.
- Select ruff's unused-argument and blind-except rules, which widens the lint policy.
- Have a migration declare the names it retires, and fail a census once its last slice lands.
- Require a cleanup PR to name the check that fails on the next instance of its class.

Revisit the reviewer's category if, over 50 reviewed batches, fewer than one `design` finding
in five reaches the fix round, or if the operator still finds the listed shapes in merged
fan-out PRs.

## Governs

No `DECIDED:` marker carries this decision. Its text is the list at
`scripts/dev/fanout_lib/brief.py:DESIGN_RULES` and the `design` paragraph of
`scripts/dev/fanout_lib/review/review_system_prompt.md`.
