# CI throughput: what remains

A brief for the session that takes up [CI throughput](ci-throughput.md) after
2026-11-14. That page holds the plan, the measurements and every ruling. This page
holds what a session that was not there needs: the state, the work left in order, and
the traps the plan's text does not carry. Delete an item here in the PR that finishes it,
and delete the page with the last one.

## State on 2026-11-15

Phase A, R1, C1, B2 and B3 are landed, and so is D9. Every landing takes the landing turn
and forms a batch, sessions open their PRs as drafts, and a fan-out batch on daniel-box
lands its PR as a draft. A fan-out batch on daniel-server still marks its PR ready and
waits for that run, which the operator chose. Phase C stopped at C1.

The rulings in force, all in [CI throughput](ci-throughput.md):

- **D5.** The credential that registers a runner stays on daniel-box.
- **D8.** The queue's run replaces the pull-request run, and Phase C stops at C1. Look
  at the test set-up before reopening C2.
- **D9.** Every landing forms a batch, and PRs are opened as drafts.
- **Option A, under D9.** daniel-server fan-out batches keep a run of their own.

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
- **A red batch.** The holder's log reads `the batch is not green`, and each member's log
  reads `a batch that tested PR #N was not green; it lands alone`. A member that is
  stacked a second time by a later landing is a defect in `land_lib/line.py`'s answers.

### 2. B4: every landing through the queue

The plan's text: the merge pacing is deleted, the branch update that follows a green run
on a stale base is deleted, `LAND_QUEUE` goes with them, and the limit on live landings
becomes one count across both hosts that a landing takes when it gets the turn.

That text predates D9. Settle these three questions before writing code, and write the
answers into [CI throughput](ci-throughput.md):

- **What tests a PR that was opened ready?** `land_lib/stale_base.py` re-tests a green
  head whose base master moved past (#4013: red master runs followed green PRs). A draft
  gets that test at its turn. A Renovate PR and a PR a person opened are ready and green
  before any landing starts. Deleting the update for them is safe only if something else
  tests them on the master of the turn.
- **Is the pacing redundant?** `land_lib/pace.py` holds a merge while master runs are
  unfinished. With one merge at a time and master's run proven by the tested tree, a
  master run takes 13 to 17 seconds. Measure how many master runs still sweep in full
  before deleting the hold: a landing that skips the turn (`LAND_QUEUE=0`) and a merge
  made by hand both cause one.
- **Where does the one count live?** `land_lib/live_limit.py` keeps a slot registry under
  `/tmp` on each host. The turn's lock is on daniel-box and daniel-server reaches it over
  ssh (`land_lib/turn.py`). A count taken at the turn has to say what a landing does when
  daniel-box does not answer.

B4 edits the merge wait in `land_lib/merge.py`, which also holds the draft-skip grace,
the re-trigger and the rate-limit backoff. Cut it into PRs that each delete one thing.
For each deletion, break the code beside it on purpose and see a test fail: a deletion
that took too much passes every test that did not exercise it.

### 3. Option B, if the operator asks for it

A daniel-server fan-out pipeline lands its own draft from daniel-server. From 2026-11-08
to 2026-11-14, 57% of review batches ran there. The code change is small
(`scripts/dev/fanout_lib/brief.py:lands` and `review_ci.py`). Two rulings come first and
both are the operator's: it reverses the rule that only daniel-box lands, and a landing from
daniel-server ends `deferred` when its health gate grades a container on daniel-pi
(#8773).

### 4. Phase C, only if D8 is reopened

C2 and C3 are in [CI throughput](ci-throughput.md) as planned. ADR-0032 holds the five
conditions a runner exists under. A runner configuration must never reach a session's
screen, and the operator mints or approves anything that touches the registration
credential.

## Traps the plan's text does not carry

- **A batch stacks only a draft whose head has had no CI run that ran jobs.** A green PR
  converted to draft does not qualify, and its landing spends a full run marking it ready.
  To qualify one, run `gh pr ready --undo <n>` and then `gh pr update-branch <n>`.
- **A PR that changes landing code lands alone.** Landing code is every module the
  landing process loads (`land_lib/own_checkout.py`), which reaches `renderer_lib` and
  `promote_lib`, 205 files on 2026-11-14. Land such a PR with `LAND_BATCH=0`. A landing
  already in flight from older code refuses once the PR merges, and the remedy is the
  same command with `--fresh`.
- **Never hold the landing turn by hand.** Two hand-held turns on 2026-11-14 stopped
  every landing for 56 and 92 seconds when a landing they waited for never arrived. A
  burst of landings forms its batches behind its first landing without help.
- **A landing started on daniel-server needs `--fresh`** when that host's primary
  checkout lags master, and it holds one of three slots on that host until its verdict.
  `land.sh` exiting 75 with `landings are already live` means wait and re-run.
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
