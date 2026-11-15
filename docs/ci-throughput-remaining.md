# CI throughput: what remains

A brief for the session that takes up [CI throughput](ci-throughput.md) after
2026-11-14. That page holds the plan, the measurements and every ruling. This page
holds what a session that was not there needs: the state, the work left in order, and
the traps the plan's text does not carry. Delete an item here in the PR that finishes it,
and delete the page with the last one.

## State on 2026-11-15

Phase A, R1, C1, B2, B3 and B4 are landed, and so are D9 and D10. Every landing takes the landing turn
and forms a batch, sessions open their PRs as drafts, and a fan-out batch on daniel-box
lands its PR as a draft. A fan-out batch on daniel-server still marks its PR ready and
waits for that run, which the operator chose. Phase C stopped at C1.

The rulings in force, all in [CI throughput](ci-throughput.md):

- **D5.** The credential that registers a runner stays on daniel-box.
- **D8.** The queue's run replaces the pull-request run, and Phase C stops at C1. Look
  at the test set-up before reopening C2.
- **D9.** Every landing forms a batch, and PRs are opened as drafts.
- **Option A, under D9.** daniel-server fan-out batches keep a run of their own.
- **D10.** B4's shape: a stale ready PR rejoins the line, pacing goes, and one count of 6
  is taken after the merge.

## The work left, in order

### 1. Three paths that have not run live

Each needs traffic no session controls, so watch for it and record the first run in
[CI throughput](ci-throughput.md).

- **A batch formed with no `LAND_BATCH` set.** Two drafts whose heads have had no CI run
  must land at once. The holder's log then reads `testing PR #a, PR #b in one run`. The
  logs are `/tmp/homelab-landings-<user>/land<pr>-*.log` on the host that started the
  landing, and a fan-out batch's is in its worktree's `.fanout/` directory.
- **A daniel-box review batch landing its draft.** Its unit's log shows no
  `cc-wait gh-ci` call, and its record under `~/.local/state/fanout-review/` carries a
  `verdict` and an empty `ci_gate`. A landing that ends `pr-ci-red` must show one `ci-fix`
  phase and a second landing (`scripts/dev/fanout_lib/review/review_ci.py:land_draft`).
  Two ran by 2026-11-15 01:00 UTC: #8805's record has a verdict and an empty `ci_gate`,
  and #8800's ended `merge-conflict`. Their unit logs were gone, so the missing
  `cc-wait gh-ci` call is unverified, and the `pr-ci-red` branch has not run.
- **A red batch.** The holder's log reads `the batch is not green`, and each member's log
  reads `a batch that tested PR #N was not green; it lands alone`. A member that is
  stacked a second time by a later landing is a defect in `land_lib/line.py`'s answers.

### 2. Option B, if the operator asks for it

A daniel-server fan-out pipeline lands its own draft from daniel-server. From 2026-11-08
to 2026-11-14, 57% of review batches ran there. The code change is small
(`scripts/dev/fanout_lib/brief.py:lands` and `review_ci.py`). Two rulings come first and
both are the operator's: it reverses the rule that only daniel-box lands, and a landing from
daniel-server ends `deferred` when its health gate grades a container on daniel-pi
(#8773).

### 3. Phase C, only if D8 is reopened

C2 and C3 are in [CI throughput](ci-throughput.md) as planned. ADR-0032 holds the five
conditions a runner exists under. A runner configuration must never reach a session's
screen, and the operator mints or approves anything that touches the registration
credential.

## Traps the plan's text does not carry

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
  checkout lags master, and it holds one of three slots on that host until its verdict.
  `land.sh` exiting 75 with `landings are already live` means wait and re-run.
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
