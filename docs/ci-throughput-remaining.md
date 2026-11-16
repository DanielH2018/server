# CI throughput: what remains

A brief for the session that takes up [CI throughput](ci-throughput.md) after
2026-11-14. That page holds the plan, the measurements and every ruling. This page
holds what a session that was not there needs: the state, the work left in order, and
the traps the plan's text does not carry. Delete an item here in the PR that finishes it,
and delete the page with the last one.

## State on 2026-11-15

Phase A, R1, C1, B2, B3 and B4 are landed, and so are D9 and D10. Every landing takes the landing turn
and forms a batch, sessions open their PRs as drafts, and a fan-out batch on daniel-box
lands its PR as a draft. A review batch on daniel-server lands its draft too, unless the
rule of the server lander holds the PR for a person. Phase C stopped at C1.

The rulings in force, all in [CI throughput](ci-throughput.md):

- **D5.** The credential that registers a runner stays on daniel-box.
- **D8.** The queue's run replaces the pull-request run, and Phase C stops at C1. Look
  at the test set-up before reopening C2. The operator kept it closed on 2026-11-15, after
  the cap bound for 24.1 minutes in bursts under load.
- **D9.** Every landing forms a batch, and PRs are opened as drafts.
- **Option B, 2026-11-15.** A daniel-server review batch lands its own draft, unless the
  rule of the server lander holds the PR. It replaces option A.
- **D10.** B4's shape: a stale ready PR rejoins the line, pacing goes, and one count of 6
  is taken after the merge. Amended on 2026-11-15: one that went stale while it waited for
  the turn gives the turn back and rejoins too.
- **Option B's preconditions, 2026-11-15.** The health gate on daniel-server reads a Pi
  container through daniel-box.

## The work left, in order

### 1. A red batch, which has not run live

It needs traffic no session controls, so watch for it and record the first run in
[CI throughput](ci-throughput.md). The holder's log reads `the batch is not green`, and each
member's log reads `a batch that tested PR #N was not green; it lands alone`. A member that
is stacked a second time by a later landing is a defect in `land_lib/line.py`'s answers.

### 2. Phase C, only if D8 is reopened

The measurement after option B and the turn fixes found 18.4 minutes at the cap in four
hours, in bursts (*After option B and the turn fixes, measured* in
[CI throughput](ci-throughput.md)). Reopening D8 on it is the operator's decision. C2 and C3
are in [CI throughput](ci-throughput.md) as planned. ADR-0032 holds the five
conditions a runner exists under. A runner configuration must never reach a session's
screen, and the operator mints or approves anything that touches the registration
credential.

## Traps the plan's text does not carry

- **A landing's log lives in the `$TMPDIR` of whatever started it.** A session's
  `land.sh --detach` writes `land<pr>-*.log` under its job's `~/.claude/jobs/<id>/tmp/`, other
  callers under `/tmp/homelab-landings-<user>/`, and a fan-out batch in its worktree's
  `.fanout/` directory. Each is on the host that started the landing, and a job's directory
  goes when the job is deleted, so copy a log before counting from it.
- **A batch stacks only a draft whose head has had no CI run that ran jobs.** A green PR
  converted to draft by hand does not qualify. The landing qualifies a green ready PR that
  master moved past by itself (`stale_base.rejoin_line`).
- **A PR that changes landing code forms no batch of its own.** Landing code is every
  module the landing process loads (`land_lib/own_checkout.py`), which reaches
  `renderer_lib` and `promote_lib`, 205 files on 2026-11-14. Land such a PR as a draft
  through the turn with `LAND_BATCH=0`; another landing may still stack it, which D10
  allows. A landing already in flight from older code refuses once the PR merges, and the
  remedy is the same command with `--fresh`.
- **Never hold the landing turn by hand.** Two hand-held turns on 2026-11-14 stopped
  every landing for 56 and 92 seconds when a landing they waited for never arrived. A
  burst of landings forms its batches behind its first landing without help.
- **A landing started on daniel-server needs `--fresh`** when that host's primary
  checkout lags master. It takes one of the 6 slots on daniel-box over ssh, and one of 3
  on its own host only when daniel-box does not answer.
- **A docs-only landing takes no landing slot.** A landing takes its slot from step 2
  (`land_lib/pipeline.py:_SLOT_FROM_STEP`), and a docs-only PR ends at step 1 with
  `nothing-to-deploy`. Such a landing exercises the turn and the serial check, never the
  slot.
- **A scratch loop that runs the suite under a timeout must kill the process group.** A
  mutation loop left an orphaned pytest that reached 23.5 GB and stalled daniel-server
  on 2026-11-14. Start the run in its own process group, kill the group on timeout, pass
  `-n 0`, and cap the address space.
- **A command handed to the operator names its directory.** A `sops unset` handed over
  with no `cd` ran in daniel-box's primary checkout on 2026-11-14. Any change there stops
  `homelab-checkout-sync` until the file is restored. Hand over
  `cd <worktree> && <command>`, and check what it changed.
- **`secret_rotation.py sync` reports a removed secret and leaves its row.** A removal
  also needs the row deleted by hand and the name added to `RETIRED` in
  `scripts/secrets_mgmt/git_dates.py`.

## See also

- [CI throughput](ci-throughput.md) holds the plan, the measurements and the rulings.
- [Landing a merged PR](landing.md) holds the turn, the line and the batch as built.
