# G9 — The notice blocks, and an ack is a line in a commit message

Status: IN PROGRESS. Step 1, the comparison, landed as the fix for
[#8419](https://github.com/DanielH2018/server/issues/8419).

Depends on: [G7](g7-removed-names.md), whose doc predicate the replay reads.

Unblocks: [G11](g11-binding-sweep.md), which binds sentences to this gate.

[ADR-0031](../../adr/0031-each-kind-of-fact-has-one-stateless-detector.md) records the
decision this slice serves. The operator ruled on 2026-11-13 that the gate may block
merges again under the two thresholds below.

## Goal and exit criterion

The notice that G1 built reaches no session: it writes to a CI job summary. This slice
gives it an exit code. The gate keeps no state. It reads one PR's diff, as the notice
does, so it brings back none of the lock's costs that
[ADR-0023](../../adr/0023-fact-support-is-a-diff-notice.md) records.

Exit: a PR that changes a cited symbol fails a CI step that names the citing section. The
step passes when the PR edits the section, or when a commit message carries the section's
ack line.

## Baseline at `4d85f67c5`

- The 30-day replay showed 90 moved rows, and 47 read as edited in the same commit.
- The notice compared raw section bodies, so a regenerated `## At a glance` block or a
  renamed heading read as a prose edit
  ([#8419](https://github.com/DanielH2018/server/issues/8419)). Step 1 corrected the
  comparison. The correction changed no row in the window: the replay at `c9de2bd37`
  showed 48 of 93 moved rows edited, before it and after it. The split the earlier
  decisions read was therefore not inflated by this defect.
- Nobody has measured how many unedited rows left a false sentence behind. The G3 build
  rule compared prose-edit ratios, which cannot show that.

## Design

- **The comparison reads hand-written prose.** Step 1 drops generated blocks from both
  bodies before comparing, so a hook's rewrite of a block clears nothing. A section whose
  heading the base lacks reads as unedited when a base section holds the same prose, which
  is a renamed heading. A section the PR wrote reads as edited.
- **Measure.** Re-run the 30-day replay over the doc set G7 defines. Grade every unedited row and a sample of the edited rows. The gate blocks only
  when both thresholds hold:
    1. at least 20% of the graded unedited rows left a false sentence;
    1. the projected acks stay below the 116 lock commits in 30 days that ADR-0023 records.

  Project acks per content citation, scaled to the number of content citations G11 aims
  for, because G11 multiplies what the gate reads. When a threshold fails, the slice stops
  here and the notice stays advisory.
- **Rule.** `fact_status.py notice --since <base> --gate` exits 1 when a section that
  cites a moved atom is unedited and unacked.
- **Ack.** The tool prints each section with a short id derived from its key. One line
  covers every moved atom in that section: `Fact-Ack: <id> <reason>` in any commit message
  of the PR. Pushing that commit re-runs CI. `Removes-Object:` is the precedent:
  `scripts/lib/removes_object.py:declared` matches its line anywhere in a message.
  `squash_body` in `scripts/deploy_tools/land_lib/merge.py` writes a body when a commit
  carries either line, so master's history holds every ack.
- **Where it runs.** A step in the existing `pr_gates` CI job, which fetches full history
  and runs on PRs only. No job is added, because the aggregate job reads a job skipped on
  a push as a failure. The step runs the merge base's copy of `fact_status.py`, as the
  notice step does, so a PR cannot edit its own gate.
- **Before the PR opens.** `scripts/dev/repo_guards.py` runs the same command. Delivery
  does not depend on it: a red check reaches every session that lands a PR.
- **Renovate.** No bump PR moves a cited atom while no `customManagers` pattern selects a
  Python file, and the `renovate-pin` rule covers the YAML keys. One census row fails if a
  pattern ever selects a `.py` file.
- **A failing fact test names its sections.** A pytest hook prints the `# fact:` sections
  of a failing test.
- **Audit.** `report`'s replay counts gated sections on master by outcome: edited, acked,
  neither, or unknown for a commit that did not come through land.sh's squash. Neither
  must be zero. `homelab-review` grades a sample of edited and acked sections each run and
  files a finding for each stale one.
- **A revert PR** that moves cited atoms needs acks like any other PR.

```diff
 scripts/lib/facts/notice.py
+  edited: prose compared without generated blocks; a renamed heading is unedited (step 1)
+  a short id per section; --gate: exit 1 on a section neither edited nor acked
 .github/workflows/ci.yml
-  step "Notice: CLAUDE.md sections citing atoms this PR moved"  (continue-on-error, hooks job)
+  step in pr_gates: notice --gate, from the merge base's code; blocking after step 5
 scripts/dev/repo_guards.py
+  the same command, before the PR opens
 scripts/deploy_tools/land_lib/merge.py
+  squash_body writes a body on Removes-Object or Fact-Ack
 scripts/lib/facts/replay.py
+  gated sections by outcome: edited, acked, neither, unknown
 census rows
+  no renovate customManagers pattern selects a .py file
 conftest
+  a failing test that carries "# fact:" prints the sections it supports
+docs/adr/00NN-…   amends ADR-0023: the measured figures, the projection, the exit rule
```

## Steps, as PRs

1. The comparison fix, and the 30-day replay re-run with it
   ([#8419](https://github.com/DanielH2018/server/issues/8419)). Done: the replay's counts
   did not move.
2. The grading of the unedited rows and the ack projection, recorded in this file. The
   slice stops here when a threshold fails.
3. `--gate`, the section id, the ack line and `squash_body`, with the CI step behind
   `continue-on-error`.
4. The replay's outcome counts, the census row and the pytest hook.
5. The ADR that amends ADR-0023, and the removal of `continue-on-error`. This step waits
   until the merge base carries `--gate`, because the step runs the base's code. From then
   on a crash fails the check.

## Verification

Change a cited symbol on a branch, open the PR, and read the failed step's rows. Push a
commit whose message carries the ack line and watch the step pass.

## Rollback

Restore `continue-on-error` on the step. The ADR for step 5 records when the gate returns
to advisory: a graded yield under 20% over 60 days, or acks above the ceiling. The
projection is re-checked after G11's first batches.

## Risks and what is not verified

- The share of unedited moved rows that left a false sentence. Step 2 measures it.
- The count of content citations G11 produces, which the ack projection scales to.
- A trivial edit, a value-fixer rewrite or a rote ack satisfies the gate. So does a
  paragraph moved into another section, which reads as edited there. The audit samples
  edited and acked sections.
- A PR green at its base misses a section that master adds afterwards.

## Complexity delta

Adds one flag, one CI step and one line format. Removes the advisory step and the row in
the root `CLAUDE.md` that tells a session to run the notice by hand.
