---
id: "0023"
title: Fact support is a diff notice, and the lock is retired
status: Accepted
date: 2026-10-17
governs:
  - ".github/workflows/ci.yml#the notice never blocks a merge"
---

# ADR-0023: Fact support is a diff notice, and the lock is retired

Amends [ADR-0019](0019-fact-support-stops-at-the-repo-store.md). ADR-0019's scope still
stands: fact support covers `CLAUDE.md` sections only. What changes is how a section learns
that a diff moved an atom it cites. ADR-0019 keeps `docs/facts.lock` and its blocking hash
test. This record replaces both with the diff notice that slice G1 built.

## Status

Accepted. The operator accepted it on 2026-10-17 in PR 1's review. The words "I accept
everything in #2" answered a list of six rulings, and so accepted each one:

1. The five-day window, with its 4 graded content moves, is G1's parity evidence, in place
   of the plan's 30 days and about 5 moves.
2. The notice keeps `gone` rows.
3. The reconstructed `notice_replay` JSON below meets G1's requirement for a verbatim
   record.
4. Content atoms lose their blocking coverage, which failure class 8 names.
5. Agent sessions cannot read the notice until slice G3 lands, except by running it
   locally or reading the check-run annotations.
6. This record takes the number 0023.

Slice G2's later PRs, which delete the lock, merge only after master carries this record.
The plan is [g2-retire-lock.md](../target-architecture/docs-guard/g2-retire-lock.md).

The plan named this record ADR-0020. D1's ADR took 0020 first, and ADR-0021 and ADR-0022
followed it, so this record takes the next free number.

## Context

**ADR-0019's criterion is not met.** ADR-0019 retires the lock when `fact_status.py report
--days 60` shows fewer than two actual moves across the symbol, YAML, test and marker forms.
At `70591c307` the content forms showed 10 potential moves and 3 actual. PR 1 ran the report
again on 2026-10-17 at 21:55Z, on `origin/master` at `b645456b8`. The lock is younger than
the 60-day window. The precision table and the replay summary read:

```text
Lock precision since 2026-08-18T21:55:54Z (60 days, 187 lock commits)
  form     potential  actual  ratio
  path            27       3  0.11
  symbol           4       2  0.50
  yaml             4       3  0.75
  test             7       2  0.29
  marker           0       0  -

Notice replay over the window, which the lock is younger than
  commits in the window       3013
  commits carrying a notice   36
  notices, by kind            gone 4, moved 40
  prose-edited, by kind       gone 2, moved 27
  moved rows                  40 (27 with the section's prose edited)
  crashes                     0
  content-form lock moves     15: listed 15, hashing 0, split 0, miss 0
  path-form lock moves        27 (context: a path atom hashes its path, so no diff moves it)
  lock commits where a row's python changed  0 (context: precision skips their rows)
```

The content forms show 15 potential moves and 7 actual, so the criterion does not fire. The
lock still finds prose fixes. The output above omits three blocks that do not bear on the
decision: the 27 path-form moves by commit, the backlog of sections by doc reads, and the
instruction bytes per session.

**The lock's cost.** At `70591c307`, 116 of the 1,282 commits in 30 days were lock commits.
A Renovate PR that bumps a cited YAML key stays red until a person commits a `verify` to it
(#4012). Master push run 35729601483 failed on 2026-09-22, on head `b86d87d60`, because the
lock test `ansible/tests/repo/test_facts_lock_matches_tree.py` raised an AssertionError on
`ansible/roles/setup/k3s/CLAUDE.md#Autonomous-role contract`. The coordinator checked that
run's log on 2026-10-17.

**G1's soak passed over a five-day window, not 30 days.** The operator shortened the soak to
five days on 2026-10-17 (#5470). The window ran from `<ts>`, 2026-10-12T21:14:44Z, to
2026-10-17T21:14:44Z. The window held 4 graded content moves. G1's plan priced a sample of
about 5 a month, so this sample is close to one month's count, gathered in a sixth of the
time. A parity miss that happens about once a month could go unseen in it. The checks ran
from 21:46Z to 21:52Z, and their output is on #4738:

- **`<ts>` did not move.** `git log --since=2026-10-12T21:14:44Z --
  scripts/lib/facts/notice.py` prints nothing, so no PR moved `<ts>` and no mechanical
  `notice.py` change landed. `ci.yml` has 28 commits since `<ts>`. The notice step, the
  "Notice step failed" step and the upload step's keys are byte-identical at `a106c04` and
  at the run's HEAD. `3b11cce69` split `docs/facts.lock` into a directory. It changes neither
  `notice.py` nor a CI step, and the replay still detects moves in the split layout.
- **Check 1, the replay: PASS.** `report --days 5 --json` covered 351 commits. 8 of them
  carried a notice, all `moved`, and 7 of those had the section's prose edited. 0 crashed.
  It found 4 content-form lock moves, `fa09d3a08`, `2226b7b61`, `529b695c1` and
  `63c40007c`. All 4 are `listed`, with 0 `miss`, 0 `hashing` and 0 `split`. There were no
  path-form moves. The run started 31 minutes after the window ended, so `--days 5` drops
  the first half hour after `<ts>`. A `--days 6` run covers it, and those commits carry no
  content move.
- **Check 2, no CI crash: PASS.** No `notice-step-failed` artifact was created after `<ts>`.
  Two exist, both from #4732's own pre-merge proof that the step's failure path uploads, at
  2026-10-12T20:48:28Z and 20:52:46Z. They show that the query is not vacuous. 1002 PR runs
  fell inside the window.
- **Check 3, delivery: PASS, 4 of 4.** Each move's PR run used a base containing `<ts>`,
  parsed at least 919 citations, and carried a notice titled with the moved unit. No run was
  `capped`, and no run, move or commit was named rather than graded.

| Commit | PR | Citations parsed |
|---|---|---|
| `fa09d3a08` | #4771 | 919 |
| `2226b7b61` | #5369 | 1068 |
| `529b695c1` | #5412 | 1068 |
| `63c40007c` | #5439 | 1101 |

No content-form `miss` and no hashing-code `miss` needs explaining here.

**The `notice_replay` JSON.** `report` cannot re-run an earlier window, and #4738 records
the figures but not the JSON. The JSON below is a reconstruction. PR 1 ran on 2026-10-17 at
21:55Z, calling `report.precision` and `replay.notice_replay` directly with `now` pinned to
the window's end. The walk still runs to HEAD, and it reads the last lock commit before the
window, `cfe3bfa43`, as its start. It therefore counts 358 commits rather than 351, and 9
notices rather than 8. The extra notice is `cfe3bfa43`'s own, committed at 21:08:16Z, before
`<ts>`. The four content-form moves match the graded run exactly.

```json
{
  "start": "cfe3bfa43869db76afd5613d897847ca2fcfddc0",
  "commits": 358,
  "commits_with_notice": 9,
  "notices": {"moved": 9},
  "prose_edited": {"moved": 8},
  "moved": 9,
  "moved_prose_edited": 8,
  "crashed": [],
  "content_lock_moves": [
    {"commit": "fa09d3a089e0792f3430ec9c3c5a0d507c7d6b0f", "committed_at": "2026-10-12T22:53:42Z", "unit": "ansible/roles/k8s/observability/CLAUDE.md#One config change, one restart", "atom": "ansible/roles/k8s/observability/defaults/main.yml:observability_stabilise_workloads", "class": "listed"},
    {"commit": "2226b7b6169ff010ad3dd5b29efa65344acbebf8", "committed_at": "2026-10-17T13:52:50Z", "unit": "ansible/roles/k8s/observability/CLAUDE.md#Flux in the metrics plane", "atom": "ansible/roles/k8s/observability/defaults/main.yml:observability_ksm_flux_resource_state", "class": "listed"},
    {"commit": "529b695c10ad9717b29babc77898b64adf35f33e", "committed_at": "2026-10-17T14:32:35Z", "unit": "ansible/roles/k8s/node-exporter/CLAUDE.md#The textfile collector directory", "atom": "ansible/roles/k8s/monitor-bridge/files/verdicts/host_cgroups.py:claude_cgroup_verdict", "class": "listed"},
    {"commit": "63c40007c8f9e46a3f7aa3b2d6f7ee0279cc4fab", "committed_at": "2026-10-17T20:23:49Z", "unit": "ansible/roles/k8s/registry/CLAUDE.md#Notable", "atom": "scripts/renderer/renderer_lib/images.py:ghcr_refs", "class": "listed"}
  ],
  "content_notices": [
    {"commit": "cfe3bfa43869db76afd5613d897847ca2fcfddc0", "unit": "ansible/roles/k8s/flux/CLAUDE.md#How the render meets the fleet guards"},
    {"commit": "fa09d3a089e0792f3430ec9c3c5a0d507c7d6b0f", "unit": "ansible/roles/k8s/observability/CLAUDE.md#One config change, one restart"},
    {"commit": "aba4c2e7bc6f7ca6ebe8c7d5d8f65965f8446ab1", "unit": "ansible/roles/setup/homelab_builder/CLAUDE.md#The dispatcher and the arm"},
    {"commit": "b7d8f0ef5bc4f2e415e948eec158d141b7c37f7b", "unit": "ansible/roles/setup/homelab_builder/CLAUDE.md#What the builder half writes (H3.5a)"},
    {"commit": "e9cb97abb4ac93b55c48412d2c8587677c4bb5f1", "unit": "ansible/roles/k8s/manifests/CLAUDE.md#Rules a caller can break"},
    {"commit": "e59b5dde918eba2b432604a43c093a0cd16ed50e", "unit": "ansible/roles/k8s/flux/CLAUDE.md#How the render meets the fleet guards"},
    {"commit": "2226b7b6169ff010ad3dd5b29efa65344acbebf8", "unit": "ansible/roles/k8s/observability/CLAUDE.md#Flux in the metrics plane"},
    {"commit": "529b695c10ad9717b29babc77898b64adf35f33e", "unit": "ansible/roles/k8s/node-exporter/CLAUDE.md#The textfile collector directory"},
    {"commit": "63c40007c8f9e46a3f7aa3b2d6f7ee0279cc4fab", "unit": "ansible/roles/k8s/registry/CLAUDE.md#Notable"}
  ],
  "path_lock_moves": [],
  "python_changed": []
}
```

**`gone` rows.** The G1 window held no `gone` row. The 60-day replay above holds 4, and 2 of
those came with a prose edit to the citing section.

**Agent sessions do not read the notice.** `gh pr checks` and `gh run view` show neither the
job summary nor the annotations. During G1 that cost nothing, because the lock still blocked.

## Decision

The lock is retired. `docs/facts.lock`, the `verify`, `reverify`, `forget` and `status`
subcommands, the `facts-reverify-changed` hook and the hash test
`test_every_recorded_atom_hashes_as_recorded` are deleted. `fact_status.py lint` stays, and a
suite test runs it over every section, blocking on every error rule rather than only on
citation resolution. The diff notice is the only signal that a diff moved a cited atom, and
it never blocks a merge.

ADR-0019's criterion did not decide this. The report shows 7 actual content moves in 60 days,
and the criterion fires below two. The lock is retired because the notice measured the same
moves without it: every one of the 15 content-form lock moves in 60 days was `listed`, with
no `miss`. The notice covers the sections that ADR-0019's criterion was measuring.

The notice keeps `gone` rows. They cost no annotation slot, because path rows sort after
content rows. Of the 4 in the 60-day replay, 2 led to a prose edit. G1's own window held
none, so this rests on the replay alone.

## Consequences

**A moved atom no longer blocks a merge.** A PR that moves a cited symbol, YAML key, test or
marker gets a green check and a notice. Failure class 8, prose that nothing re-derives, loses
its blocking coverage for content atoms. The whole-tree lint test keeps blocking on what it
can derive from one commit: a citation that no longer resolves, an ambiguous marker prefix, a
retired host and a Renovate-managed citation.

**The notice does not depend on GitHub.** Its source is `fact_status.py notice --since
origin/master`. G1's CI step runs that command against the base it computes, once per output
format. The job summary and the check-run annotations are two views of one output.

**An agent session must fetch the notice.** It runs the command locally, or reads the
annotations through the check-runs API. Slice G3 is the remedy that brings the notice to a
reviewer without either step.

**The yield measurement moves to the replay.** `report`'s notice replay over a plain `--days`
window measures the notice from now on. G3 is the escalation path if that yield falls.

**The five-day sample is small.** Four graded moves in five days is the evidence for parity.
A rare class of miss could appear only after the lock is gone, and nothing would then turn it
red.

**Reversing this record costs more the longer it stands.** It means restoring the lock from
git history and re-verifying every section whose atoms moved since. *Rollback* in the G2 plan
gives the order.

## Governs

The `DECIDED:` marker on the notice step's comment in `.github/workflows/ci.yml`, which says
that the notice never blocks a merge.
