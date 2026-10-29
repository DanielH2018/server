---
name: land-after-merge
description: Merge a PR and follow it through to a verified deploy with `land.sh`. Use when a PR is ready to merge, has just merged, or when you need the exact merge → CI-wait → tick → deploy → verify sequence. Covers what `land.sh` does, its VERDICT line, and why hand-polling CI and hand-merging are wrong here.
allowed-tools: Bash, Read, Grep, Glob
---

The procedure `CLAUDE.md` → *After a PR Merges — Pull, Deploy, Verify* points at. That section
owns the two things that must stay resident — the standing directive that a merge is followed
through without asking, and the *When to wait* list. This skill owns the invocation. The
reasoning behind every rule below, and the incident that put it there, is
[docs/landing.md](../../../docs/landing.md).

## The invocation

One command. Do not write the redirect and do not poll CI.

```bash
./scripts/deploy_tools/land.sh --pr <n> --arm-merge --await-merge --detach && cc-wait land <n>
```

It merges the PR directly once its CI is green, waits for master CI on the merge
commit, deploys that commit, kicks the tick, gates the health, and prints the landing's
`VERDICT:` line. `--detach` names its own logfile, forks into it and returns. `cc-wait land <n>`
is the wait: it prints the landing's `VERDICT:` line and exits with the landing's own code.
`land.sh` arms no PR for GitHub's auto-merge. Its direct merge is pinned to the head SHA it
checked, which auto-merge cannot do (`docs/landing.md` has the detail).

**Auto-merge is allowed, and it is not a landing.** `gh pr merge --auto --squash <n>` merges the
PR once CI is green, from the operator's account or the agent's, with no review and no bypass.
Use it when the merge should not wait on your session. Then run the command above as usual:
`--arm-merge` on a merged PR is a no-op, and the landing deploys the merge commit.

**A stale base costs one branch update first.** Before the merge, `land.sh` asks whether master
changed any path since the base the head's green CI run tested it against (the PR's merge base
when that run's log cannot be read). When it did, it merges master into the PR branch
once, prints `updating its branch so CI re-tests it against master before the merge`, and
merges only after CI is green on the updated head. That is the normal path for about half of
PRs and adds a CI run's worth of time, not a fault. The exception is a head the operator
approved: the update would void the approval, so `land.sh` refuses it and names the remedy,
which is to rebase onto origin/master, push and get the new head approved.
`land_lib/merge.py`'s module docstring owns the rule.

**A master CI backlog holds the merge.** While any master `ci.yml` run is unfinished,
a green PR waits to merge and `land.sh` prints `pacing the merge: …`. Merging faster than
master CI clears only deepens the runner queue every landing waits on (#5526). The wait does
not count against the merge budget, and the cap rises by one per 30 minutes held, so a landing
queued behind others merges in turn rather than ending at `merge-timeout` (#6466).
`LAND_PACE_MASTER_RUNS` sets the cap and `0` turns pacing off; `land_lib/pace.py` owns
the rule.

`cc-wait` waits at most 570s per run. Exit 75 means the landing is still running: re-run only
`cc-wait land <n>`, never `land.sh`, which would start a second landing. State `gave-up` (exit 3)
is `land.sh`'s own give-up, a resume point for the whole command. State `deferred` (exit 4) is
not: the PR landed, and the next tick applies it, so there is nothing to re-run. Where a task notification can
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

`land.sh --help` prints the flags, the exit codes and the verdicts.

```bash
./scripts/deploy_tools/land.sh --pr <n> --tags sonarr,radarr ...   # skip derivation, scope by hand
./scripts/deploy_tools/land.sh --pr <n> --any-author ...           # lift LAND_REQUIRE_AUTHOR
./scripts/deploy_tools/land.sh --pr <n> --subject "..." ...        # override the squash subject
```

**Without `--detach` you own the redirect.** `> "$CLAUDE_JOB_DIR/tmp/land<n>.log" 2>&1` is not
optional: Ansible refuses to start on the non-blocking pipe a backgrounded Bash call hands the
script, and the error names Ansible rather than the harness. Then wait with
`cc-wait file <log> --match '^VERDICT:' --fail '^Traceback'` and **do not end your turn** on the
landing.

## Two command shapes the classifier refuses, and what to write instead

Both are about the command's TEXT, not what it does. Keep prose out of the command string.

**Open the PR with `--fill` and a body file.** `gh pr create --title "…"` is refused
whenever the title carries `git`, `cd`, `worktree`, `write` or a construct the containment check
cannot parse, and a title here routinely names one. `--fill` takes the title from the commit, and
gh lets `--body-file` override the body it would fill. Write the file to
`.github/pull_request_template.md`'s sections first: `block-footguns` denies a create or edit
whose body skips one of its `## ` headings, and a bare `--fill` with it.

```bash
gh pr create --fill --body-file <path>   # title from the commit, body from the file
```

**A body's only closing keyword must be its own `Closes #N` line.** `--arm-merge` refuses
otherwise and prints the offending line, because GitHub closes an issue named after
`close`/`fixes`/`resolved` however the sentence reads. Reword to `Filed for later: #N` with
`gh pr edit <n> --body-file`, then re-run the same landing command — the arm is idempotent.

**After a squash merge, reset the branch before any follow-up commit.** Run
`git fetch origin && git reset --hard origin/master` in the worktree, then commit the follow-up
work. A plain push from the old branch recreates the deleted remote branch with the
pre-squash commits, so the follow-up PR opens CONFLICTING and GitHub runs no checks on it
(PR #543).

## Reading the verdict

The `VERDICT:` line is the last line of the log. `land.sh --help` lists the closed vocabulary;
`land_lib/outcome.py:Verdict` is where it is defined. Seven of them need a decision from you:

| Verdict | What to do |
|---|---|
| `settled` | Exercise the thing you actually changed. The gate sees the workload, not your change. |
| `needs-manual-apply` | **Read every remediation line, not the first.** One PR can reach two, and each prints its own. |
| `deferred` | Nothing is wrong; the next tick applies it. Re-run only if the line says this run stopped watching a tick mid-apply. With cause `promote-timeout`, no promotion covered a flux-owned tag in time: read the `promote` workflow's failed `publish` jobs first. If the line says the health gate could not read its store, the deploy went ungraded: re-run `land.sh`. |
| `deferred`, for a host | A PR that changes a host's package waits for that host to apply it. When the line names the updater's start command, the host's timer is not armed yet, or its next fire is more than 45 minutes away. Start the updater on that host, then re-run the same `land.sh` command: its first query finds the host settled. |
| `tip-outran-retries` | A resume point, not a fault in your change: master merged faster than one tick-and-deploy cycle. Re-run the same command. |
| `refused` | The landing stopped before it deployed anything, and the detail says why: the landing policy, a closed PR, changes requested, a read that failed. Fix what it names, then re-run. |
| `ci-unproven` | GitHub never ran CI for the merge commit, and no later master commit can stand in for it, so a re-run alone never converges. The detail names the tip and the way out. When no commit has run CI, dispatch it with `gh workflow run ci.yml --ref master` and re-run. When a red commit sits between the merge commit and a green one, deploy the tip by hand with the `deploy.sh --at <tip>` command it prints. |
| `handoff-timeout` | The lander unit outlived land.sh's wait and may still be running. Re-run the same command; it waits on the unit again rather than starting a second landing. |

**A tag whose `containers_list` entry sets `owner` never reaches `deploy.sh`.** `land.sh`
waits until a promotion covers the merge commit (`promote.py land-wait`) and then gates the
`owner: flux` tags. A `deploy-failed` with cause `promote-held` means `DEPLOY_HOLD` holds
promotion, and clearing it is **operator-only**: stop and name the hold. Cause
`soak-failed` means the soak Job failed on the promoted revision, which `verify` holds next:
report the services the printed `kubectl` command names, and stop. A
`nothing-to-deploy` whose detail says a tag "is suspended" means Flux applies nothing for that
tag until a PR removes its `flux_suspend`; the suspend PR itself ends this way. A PR that removes
`owner` is refused before the merge while the tag's Kustomization still exists; the handback
order is in [docs/flux-deploy-plane.md](../../../docs/flux-deploy-plane.md).
A PR that changes `cluster-vars.sops.env` or `cluster-vars.source` waits on promotion too,
for the `cluster-vars` Kustomization and every Kustomization that depends on it
(`docs/landing.md`, *A tag Flux applies waits on promotion*).

`deploy-failed` has one variant that is **not** a resume point — `a playbook task failed AFTER
applying; some changes are live`. Every other `deploy-failed`, and every give-up
(`merge-timeout`, `ci-red`, `ci-timeout`, `lock-busy`, `merge-conflict`, `pr-ci-red`), deployed
nothing and re-runs safely.

**If the task comes back "stopped because the system is running low on memory", the landing was
reaped, not failed.** Re-run the same command.

## What this skill deliberately does not carry

Every measurement and incident behind the rules above, because they are read once and then
acted on rather than needed in every session: the merge and CI wait mechanics, the stale-tree
retry arithmetic, the Pi's own deploy, the four shapes that need a hand, the annotation's
fields and the Landings board. All of it is in [docs/landing.md](../../../docs/landing.md).
This file was 418 lines and changed in 47 commits in the three months to 2026-09-30 — more than
any other file under `.claude/` — because each of those measurements landed here (#2853).

## Before you start, and when to stop

Read `CLAUDE.md` → *After a PR Merges* for the *When to wait* list and the *Working alongside
other sessions* notes. Both stay resident there deliberately: they change default behaviour, so
they have to be loaded whether or not this skill is.
