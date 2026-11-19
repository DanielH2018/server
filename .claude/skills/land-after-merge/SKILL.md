---
name: land-after-merge
description: Merge a PR and follow it through to a verified deploy with `land.sh`. Use when a PR is ready to merge, has just merged, or when you need the exact merge → CI-wait → promotion → verify sequence. Covers what `land.sh` does, its VERDICT line, and why hand-polling CI and hand-merging are wrong here.
allowed-tools: Bash, Read, Grep, Glob
---

The procedure `CLAUDE.md` → *After a PR Merges — Pull, Deploy, Verify* points at. That section
owns the two things that must stay resident — the standing directive that a merge is followed
through without asking, and the *When to wait* list. This skill owns the invocation. The
reasoning behind every rule below, and the incident that put it there, is
[`docs/landing.md`](../../../docs/landing.md).

## The invocation

One command. Do not write the redirect and do not poll CI.

```bash
./scripts/deploy_tools/land.sh --pr <n> --arm-merge --await-merge --detach && cc-wait land <n>
```

It merges the PR directly once its CI is green, waits for master CI on the merge
commit, waits on the promotion that applies it, gates the health, and prints the landing's
`VERDICT:` line. `--detach` names its own logfile, forks into it and returns. `cc-wait land <n>`
is the wait: it prints the landing's `VERDICT:` line and exits with the landing's own code.
`land.sh` arms no PR for GitHub's auto-merge. Its direct merge is pinned to the head SHA it
checked, which auto-merge cannot do (`docs/landing.md` has the detail).

**Do not arm auto-merge.** Ruleset 27167149 lets only the admin role and Renovate update
master, and GitHub's auto-merge applies no bypass, so `gh pr merge --auto` leaves the PR
`BLOCKED` from either account. `land.sh` merges as the operator, which the bypass covers.

**A stale base is tested inside the landing turn.** A head with no CI run gets its branch
updated from master when its landing takes the turn, and prints `updating its branch so CI
tests it against master before the merge`. A green head of a ready PR that master moved past
is converted to draft and updated before the landing asks for the turn, and prints `converted
it to draft and updated its branch`. One that master moved past while it waited for the turn
does the same once it takes the turn, and prints `gave the landing turn back to wait in the
line as a draft`. Each adds a CI run's worth of time, not a fault. A stale green head that
reaches the merge anyway merges as it stands, and master's own run tests it.
The exception is a stale head the operator approved: the update would void the approval, so
`land.sh` refuses it when master changed a path the PR changes, and names the remedy, which is
to rebase onto origin/master, push and get the new head approved.
`land_lib/merge.py`'s module docstring owns the rule.

**A landing waits for the landing turn before it merges.** One landing at a time, across
daniel-box and daniel-server, holds the turn. A green PR asks for it and merges; the others
print `waiting for the landing turn` once and stand in line. The wait is not a fault and
spends no GitHub calls. A PR opened as a draft gets its one CI run inside its turn, on the
master of that moment, so master's run for the merge skips its jobs. `land_lib/turn.py` owns
the rule.

**A draft that waits in line may be merged by the landing ahead of it.** The landing that
holds the turn tests the drafts standing in line with its own in one CI run and merges them
in order (`land_lib/batch.py`). Your landing then prints `the landing that holds the turn
merged PR #N with its own` and goes on to the deploy and its verdict as usual. If that
batch's run was red it prints `a batch that tested PR #N was not green; it lands alone` and
gets a run of its own. Neither line is a fault. `LAND_BATCH=0` stops one landing forming a
batch.

`cc-wait` waits at most 570s per run. Exit 75 means the landing is still running: re-run only
`cc-wait land <n>`, never `land.sh`. A second `land.sh` for a PR whose landing is live ends
`refused` with exit 75 and `cause=landing-live`, and its verdict prints the `cc-wait land <n>
--log <log>` command that waits on the live landing: run that. State `gave-up` (exit 3)
is `land.sh`'s own give-up, a resume point for the whole command. State `deferred` (exit 4) is
not: a host's updater applies its package, so there is nothing to re-run. Where a task notification can
wake the session, run the command with `run_in_background: true`. A headless `claude -p` agent
cannot be woken once its turn ends, so it runs the command in the foreground with
`timeout: 600000` and does not end its turn on it.

**To read a verdict after the wait, run `probe.py landing-verdicts <n>`**, never a grep of a
task's output file. Every landing records its `VERDICT:` line there as it exits, and the
record outlives the session.

The landing runs outside the caller's process tree, so a Bash call killed at its time limit does
not kill it. Started from a systemd user unit, such as a
fan-out batch, the landing also moves into its own `land<pr>-<pid>.scope`, so stopping the unit
does not kill it either. The log's first line names that scope, which is the way to stop the
landing itself. `--since` is
resolved from `origin/master` before the merge is armed, so you do not pass it.

`land.sh --help` prints the flags, the exit codes and the verdicts. `homelab-checkout-sync`
pulls all of master, not just your commit, so a `--tags` override names your own services only.

```bash
./scripts/deploy_tools/land.sh --pr <n> --tags sonarr,radarr ...   # skip derivation, scope by hand
./scripts/deploy_tools/land.sh --pr <n> --any-author ...           # lift LAND_REQUIRE_AUTHOR
./scripts/deploy_tools/land.sh --pr <n> --subject "..." ...        # override the squash subject
```

**Without `--detach`, run it under `runlog`.** A backgrounded Bash call hands the script a
non-blocking pipe, and a Python write that finds it full raises `BlockingIOError`. `runlog land<n> -- ./scripts/deploy_tools/land.sh --pr <n> …` gives it blocking
stdio and writes the output to a log in the session's own directory, whose path it prints
first. Then wait with `cc-wait file <log> --match '^VERDICT:' --fail '^Traceback'` and **do
not end your turn** on the landing.

## Two command shapes the classifier refuses, and what to write instead

Both are about the command's TEXT, not what it does. Keep prose out of the command string.

**Open the PR as a draft, with `--fill` and a body file.** `gh pr create --title "…"` is refused
whenever the title carries `git`, `cd`, `worktree`, `write` or a construct the containment check
cannot parse, and a title here routinely names one. `--fill` takes the title from the commit, and
gh lets `--body-file` override the body it would fill. Write the file to
`.github/pull_request_template.md`'s sections first: `block-footguns` denies a create or edit
whose body skips one of its `## ` headings, and a bare `--fill` with it.

```bash
gh pr create --draft --fill --body-file <path>   # title from the commit, body from the file
```

`--draft` is what lets the landing share a CI run. `ci.yml` runs no job on a draft, and
`land.sh` marks the draft ready or tests it with the other drafts in line. A PR opened ready
spends a full run at every push. Once it is green, its landing converts it back to draft and
updates its branch when master has moved, so it then joins the line. Do not mark the draft ready
yourself. When a PR has to show a CI result before anyone lands it, such as one waiting on
the operator's review, `gh pr ready <n>` starts its run.

**A body's only closing keyword must be its own `Closes #N` line.** `--arm-merge` refuses
otherwise and prints the offending line, because GitHub closes an issue named after
`close`/`fixes`/`resolved` however the sentence reads. Reword to `Filed for later: #N` with
`gh pr edit <n> --body-file`, then re-run the same landing command — the arm is idempotent.

**After a squash merge, reset the branch before any follow-up commit.** Run
`cd <your worktree> && git fetch origin && git reset --hard origin/master`, then commit the
follow-up work. A plain push from the old branch recreates the deleted remote branch with the
pre-squash commits, so the follow-up PR opens CONFLICTING and GitHub runs no checks on it
(PR #543).

## Reading the verdict

The `VERDICT:` line is the last line of the log. The line above it,
`LAND-RESULT: verdict=… cause=… exit=… pr=… sha=… tags=<count> hold_sha=… hold_owner=… merge=…`,
carries the same outcome as logfmt for a script to read. `hold_sha` and `hold_owner` (`own`,
`other` or `unattributed`) are set beside cause `promote-held`, and `merge` is the whole
merge commit; `docs/landing.md`, *The `LAND-RESULT:` line*, has each field. `land.sh --help` lists the closed vocabulary;
`land_lib/outcome.py:Verdict` is where it is defined. Seven of them need a decision from you:

| Verdict | What to do |
|---|---|
| `settled` | Exercise the thing you actually changed. The gate sees the workload, not your change. |
| `needs-manual-apply` | **Read every remediation line, not the first.** One PR can reach two, and each prints its own. |
| `deferred` | The landing gave up waiting. With cause `promote-timeout`, no promotion covered a flux-owned tag in time: read the `promote` workflow's failed `publish` jobs first, then re-run. With cause `gate-unreadable` or `gate-stale`, the health gate could not read its store or its tree was older than origin/master, so the deploy went ungraded: re-run `land.sh`. |
| `deferred`, for a host | A PR that changes a host's package waits for that host to apply it. When the line names the updater's start command, the host's timer is not armed yet, or its next fire is more than 45 minutes away. Start the updater on that host, then re-run the same `land.sh` command: its first query finds the host settled. |
| `refused` | The landing stopped before it deployed anything, and the detail says why: the landing policy, a closed PR, changes requested, a read that failed. Fix what it names, then re-run. When it names landing code older than master's, re-run the same command with `--fresh`. |
| `ci-unproven` | GitHub never ran CI for the merge commit, and no later master commit can stand in for it, so a re-run alone never converges. The detail names the tip and the way out. When no commit has run CI, dispatch it with `gh workflow run ci.yml --ref master`, dispatch `promote` for the tip once CI is green, and re-run. When a red commit sits between the merge commit and a green one, dispatch `promote` for the tip with the `gh workflow run promote.yml` command it prints. `promote` refuses a commit without green CI, so no route skips CI. |
| `unbuilt` | A host the landing would defer on has no package published at or after the merge, and the builder reports a failing reason, so no updater fire can apply it. Read `uv run python scripts/diagnostics/probe.py builder`, fix the builder, then re-run the same `land.sh` command. |
| `handoff-timeout` | The lander unit outlived land.sh's wait and may still be running. Re-run the same command; it waits on the unit again rather than starting a second landing. |

**`land.sh` deploys nothing itself.** It
waits until a promotion covers the merge commit (`promote.py land-wait`) and then gates the
k8s tags. A `deploy-failed` with cause `promote-held` means `DEPLOY_HOLD` holds
promotion, and clearing it is **operator-only**: stop and name the hold. To resume once the
operator clears it, or once a red master CI goes green, run `cc-wait landing-clear` in the
background and re-run the same `land.sh` command when it ends 0. It ends 1 as
`gh-unauthenticated` when `gh` has no login, which needs the operator, not a retry. Cause
`soak-failed` means the soak Job failed on the promoted revision, which `verify` holds next:
report the services the printed `kubectl` command names, and stop. A
`nothing-to-deploy` whose detail says a tag "is suspended" means Flux applies nothing for that
tag until a PR removes its `flux_suspend`; the suspend PR itself ends this way. The same wait on a
PR that also owes a plane ends `needs-manual-apply`, and one that changes a host package goes
on to the host wait.

A PR that changes `cluster-vars.sops.env` or `cluster-vars.source` waits on promotion too,
for the `cluster-vars` Kustomization and every Kustomization that depends on it
(`docs/landing.md`, *Every landing waits on promotion*).

Every `deploy-failed`, and every give-up
(`merge-timeout`, `ci-red`, `ci-timeout`, `merge-conflict`, `pr-ci-red`), deployed
nothing and re-runs safely. To find why CI went red after `ci-red` or `pr-ci-red`, run
`ghread run failures --pr <n>` or `ghread run failures <run-id>` first. It prints each failed
job's test IDs and error lines in about 1 KB, where `ghread run logs --failed` prints 40 KB
to 380 KB.

**If the task comes back "stopped because the system is running low on memory", the landing was
reaped, not failed.** Re-run the same command.

## What this skill deliberately does not carry

Every measurement and incident behind the rules above, because they are read once and then
acted on rather than needed in every session: the merge and CI wait mechanics, the stale-tree
retry arithmetic, the Pi's own deploy, the four shapes that need a hand, the annotation's
fields and the Landings board. All of it is in [`docs/landing.md`](../../../docs/landing.md).

## Before you start, and when to stop

Read `CLAUDE.md` → *After a PR Merges* for the *When to wait* list. It stays resident there
deliberately: it changes default behaviour, so it has to be loaded whether or not this skill is.
