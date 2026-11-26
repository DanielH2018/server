---
id: "0036"
title: A selected pull-request run proves its tree, and a nightly sweep is the net for a stale row
status: Accepted
date: 2026-11-19
governs: []
---

# ADR-0036: A selected pull-request run proves its tree, and a nightly sweep is the net for a stale row

## Status

Accepted. It replaces one part of the operator's ruling of 2026-11-15, recorded at the
`DECIDED:` marker on the `pytest` job in `.github/workflows/ci.yml`: that the push run on
master is the net for a module the selection left out.

## Context

A pull request's `pytest` legs run the modules its diff can affect, read from
`scripts/dev/affected_tests.json`. A row in that table can be older than a dependency its
module gained, so a selection can leave out a module the change breaks. The ruling of
2026-11-15 accepted that and named the net: a selected run recorded no tested tree for
`pytest`, and the push run after its merge repeated every `pytest` leg.

Measured on 2026-11-18, that net cost more than the selection saved. A change to one k8s
role selected about 400 of 1,469 modules and needed three of the six legs, which saved about
420 slot-seconds on the pull request. The push run then repeated the legs for about 1,050,
where a proven push costs about 394. A landing that ran CI once was therefore dearer with
the selection than without it.

The repeat also tested little the pull request had not. When the pushed tree is the tree the
pull request tested, the base and the table are the same, so a selection on the push run
would pick the modules the pull-request run already ran. What the full sweep added was only
the stale-row case.

Two options were weighed for that case. Keeping the per-push sweep is the status quo above. A
scheduled sweep finds the same module later and costs one run a day, whatever the number
of landings.

## Decision

A pull-request run whose `pytest` legs ran a selection records its tested tree as a full run
does, and the push run of that tree skips every job. The net for a stale row is the nightly
sweep in `.github/workflows/pytest-sweep.yml`, which runs every module on master's head and
reports a red run as one GitHub issue. A push whose tree no pull-request run tested still
sweeps in full.

**Extended on 2026-11-19.** A push whose tree no pull-request run tested no longer sweeps
in full. Its `pytest` legs select against the newest ancestor whose gate passed
(`scripts/dev/tested_tree.py:green_base`). The reasoning is the same: the nightly sweep is
the net for a stale row, so the per-push sweep adds nothing a selection from a green base
leaves out. The parent is not used as the base unless it is green, because a selection
against a red parent would leave out what the parent broke.

## Consequences

- A module broken through a stale row stays broken on master until the next sweep, up to a
  day, and `promote` has deployed the commits landed in between. The suite holds static
  guards, so the exposure is a guard that stopped guarding, not an untested rollout.
- A red sweep names the failed modules and not the commit. The commits landed since the last
  green sweep are the suspects.
- A red sweep holds no landing. A module that stays red loses its row the next time the
  docs-refresh cron records it, and is then selected on every run, which turns pull requests
  red until someone fixes it.
- `scripts/dev/pytest_shard.py` no longer widens a selection that needs more than half the
  legs. That rule existed to keep the tested-tree record, which a selection no longer loses.
- A landing batch still records its members only from a run that swept in full.
  **Extended on 2026-11-19:** a batch records from a selected run too. The run's diff is
  every member stacked on master, so its selection holds each module any member can
  affect. The trees between the first member's merge and the last are tested by no run,
  as they were when the batch's run swept in full.
- A push run's selection rests on its base's gate, so a gate that passed wrongly at one
  commit is trusted by every push after it until the nightly sweep runs.
- Reversing this means restoring a second status context and the `pytest-proven` output
  across `tested_tree.py`, the gate and `hosts/lib/ci_verdict.py:jobs_verdict`.

## Governs

No single line enforces this. `docs/ci-throughput.md` describes the mechanism, and
`tests/repo/ci_workflows/test_ci_skip_proven_master.py` and the `pytest-sweep-*` rows of
`tests/repo/census_rows/test_census_rows_workflows.py` hold it.
