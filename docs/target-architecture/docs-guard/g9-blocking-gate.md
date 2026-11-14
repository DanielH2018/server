# G9 — The notice blocks, and an ack is a line in a commit message

Status: STOPPED at step 2 on 2026-11-13. The measurement failed its first threshold, so
the gate is not built and the notice stays advisory. Step 1, the comparison, landed as the
fix for [#8419](https://github.com/DanielH2018/server/issues/8419).

Depends on: [G7](g7-removed-names.md), whose doc predicate the replay reads.

Unblocks: none. [G11](g11-binding-sweep.md) does not wait on this slice.

[ADR-0031](../../adr/0031-each-kind-of-fact-has-one-stateless-detector.md) records the
decision this slice serves. The operator ruled on 2026-11-13 that the gate may block
merges again under the two thresholds below.

## Goal and exit criterion

The notice that G1 built reaches no session: it writes to a CI job summary. This slice
set out to give it an exit code. The gate keeps no state. It reads one PR's diff, as the
notice does, so it brings back none of the lock's costs that
[ADR-0023](../../adr/0023-fact-support-is-a-diff-notice.md) records.

Exit, as planned: a PR that changes a cited symbol fails a CI step that names the citing
section. The step passes when the PR edits the section, or when a commit message carries
the section's ack line.

Exit, as reached: the measurement below says the gate would cost more than it finds.

## Step 2 result: the gate is not built

The replay ran at `c7723b3e7` over the 30 days to 2026-11-13, across the 109 instruction
files that G7's predicate lists.

- 1,885 first-parent commits gave 98 moved rows. The commit edited the section's prose in
  50 of them and left it alone in 48.
- **3 of the 48 unedited rows left a false sentence. That is 6%, against the 20% the gate
  needed.** Counted once per section and atom, the share is 3 of 28, or 11%.
- Every fifth edited row was graded as well, 10 rows in all. None left a false sentence
  about the atom that moved. 8 of the edits described the change that moved the atom, and
  2 were sections the commit wrote.

The three false rows:

| Commit | Section | What moved | What stayed false |
|---|---|---|---|
| `633859548`, 2026-11-01 | `chezmoi_setup`, *At a glance* | The seed test read the agent's seed from the `claude-code` component | The sentence placed the seed in the `claude_code` role. The doc was deleted on 2026-11-12. |
| `0891aba29`, 2026-11-02 | Root `CLAUDE.md`, *The procedure* | `wait_for_hosts` gained the `unbuilt` ending | The sentence named `deferred` as the only ending. A rewrite removed it on 2026-11-05. |
| `c2f162d71`, 2026-11-12 | `claude-code` component, *Autonomous-role contract* | `problems` lost the role-doc narrowing | The section described two narrowings and a held deployer path. The PR that records this result corrects it. |

The other 45 rows, by what moved:

- 19 rows: a function's body changed, and the sentence about it still held.
- 16 rows: a test's internals changed, and its name and its claim held.
- 9 rows: a registry gained or lost an entry that the sentence does not name.
- 1 row: a constant changed, and the sentence cites it by name, not by number.

**The projection.** The 48 rows fall in 47 pairs of commit and section, and one ack covers
one pair. The instruction files held 161 content citations at `c7723b3e7`, so the gate
would have asked for 0.29 acks per content citation in 30 days. The second threshold
holds at that count, with 47 acks against the ceiling of 116. It fails at about 400
content citations. A sweep that gives each of the 258 sections that cite nothing one
content citation reaches 419.

**What the result means.**

- Sessions already edit the prose when a move matters. The gate would have asked for 47
  acks to reach 3 sentences.
- None of the three sentences names a removed identifier, so G7's check reads none of
  them. They stay in the class ADR-0031 calls delivered, not proven.
- [ADR-0023](../../adr/0023-fact-support-is-a-diff-notice.md)'s ruling stands: a moved
  atom gets a notice and never blocks a merge.

**What the measurement does not cover.**

- One session graded every row, by reading the section at the commit against the atom's
  diff. No second grader checked it. The result holds if that session missed as many false
  rows as it found: 6 of 48 is 13%.
- The window held the host migration and the tree rename, which moved test internals more
  often than a quiet month does.
- A sentence that goes false in a section citing nothing about the changed code gives no
  row, so the replay cannot count it.

**Delivery.** The CI job summary reached no session. The operator ruled on 2026-11-13 that
`scripts/dev/repo_guards.py` prints the notice's unedited rows after its guards, with no
exit code. A session and a fan-out batch both run that script before they open a PR.
`fact_status.py notice --format reread` prints the same lines.

**What reopens the slice.** A later replay whose graded share reaches 20%. The report
lists the rows to grade:

```bash
uv run python scripts/dev/fact_status.py report --days 30 --json \
  | jq -r '.notice_replay.content_notices[]
      | select(.kind == "moved" and (.prose_edited | not))
      | [.commit[0:9], .atom, .unit] | @tsv'
```

## Baseline at `4d85f67c5`

- The 30-day replay showed 90 moved rows, and 47 read as edited in the same commit.
- The notice compared raw section bodies, so a regenerated `## At a glance` block or a
  renamed heading read as a prose edit
  ([#8419](https://github.com/DanielH2018/server/issues/8419)). Step 1 corrected the
  comparison. The correction changed no row in the window: the replay at `c9de2bd37`
  showed 48 of 93 moved rows edited, before it and after it. The split the earlier
  decisions read was therefore not inflated by this defect.
- Nobody had measured how many unedited rows left a false sentence behind. The G3 build
  rule compared prose-edit ratios, which cannot show that.

## Design

Only the first bullet is built. The rest describes the gate as planned, for a later replay
that reopens the slice.

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
+  each content row carries its atom, its kind and whether its prose was edited (step 2)
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
2. The grading of the unedited rows and the ack projection, recorded in this file. Done on
   2026-11-13: the first threshold failed, so the slice stops here.
3. Not built. `--gate`, the section id, the ack line and `squash_body`, with the CI step
   behind `continue-on-error`.
4. Not built. The replay's outcome counts, the census row and the pytest hook.
5. Not built. The ADR that amends ADR-0023, and the removal of `continue-on-error`. This
   step waits until the merge base carries `--gate`, because the step runs the base's
   code. From then on a crash fails the check.

## Verification

Step 2: the `jq` command above prints the 48 rows, and each graded row reads the section
at its commit against the atom's diff.

The gate, as planned: change a cited symbol on a branch, open the PR, and read the failed
step's rows. Push a commit whose message carries the ack line and watch the step pass.

## Rollback

Nothing to roll back: no gate was built. The planned rollback was to restore
`continue-on-error` on the step.

## Risks and what is not verified

- The share of unedited moved rows that left a false sentence. Step 2 measured it at 3 of
  48, by one grader.
- The count of content citations G11 produces, which the ack projection scales to.
- A trivial edit, a value-fixer rewrite or a rote ack satisfies the planned gate. So does
  a paragraph moved into another section, which reads as edited there.
- A PR green at its base misses a section that master adds afterwards.

## Complexity delta

As stopped: adds three fields to each content row of the report's JSON, one notice format
and one call in `repo_guards.py`. Removes the row in the root `CLAUDE.md` that told a
session to run the notice by hand. As planned: adds one flag, one CI step and one line format, and removes the
advisory step and the row in the root `CLAUDE.md` that tells a session to run the notice
by hand.
