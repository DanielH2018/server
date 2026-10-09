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

One command. Do not write the redirect, do not poll CI, do not run `gh pr merge` yourself.

```bash
./scripts/deploy_tools/land.sh --pr <n> --arm-merge --await-merge --detach && cc-wait land <n>
```

It merges the PR directly once its CI is green, waits for master CI on the merge
commit, deploys that commit, kicks the tick, gates the health, and prints the landing's
`VERDICT:` line. `--detach` names its own logfile, forks into it and returns. `cc-wait land <n>`
is the wait: it prints the landing's `VERDICT:` line and exits with the landing's own code.
No PR is armed for GitHub's auto-merge, because every PR into master needs a ruleset bypass
and auto-merge ignores one (`docs/landing.md` has the detail).

`cc-wait` waits at most 570s per run. Exit 75 means the landing is still running: re-run only
`cc-wait land <n>`, never `land.sh`, which would start a second landing. State `gave-up` (exit 3)
is `land.sh`'s own give-up, a resume point for the whole command. Where a task notification can
wake the session, run the command with `run_in_background: true`. A headless `claude -p` agent
cannot be woken once its turn ends, so it runs the command in the foreground with
`timeout: 600000` and does not end its turn on it.

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

**Open the PR with `--fill`, then replace the body.** `gh pr create --title "…"` is refused
whenever the title carries `git`, `cd`, `worktree`, `write` or a construct the containment check
cannot parse, and a title here routinely names one.

```bash
gh pr create --fill                 # title + body from the commit
gh pr edit <n> --body-file <path>   # then replace the body
```

**A body's only closing keyword must be its own `Closes #N` line.** `--arm-merge` refuses
otherwise and prints the offending line, because GitHub closes an issue named after
`close`/`fixes`/`resolved` however the sentence reads. Reword to `Filed for later: #N` with
`gh pr edit <n> --body-file`, then re-run the same landing command — the arm is idempotent.

## Reading the verdict

The `VERDICT:` line is the last line of the log. `land.sh --help` lists the closed vocabulary;
`land_lib/outcome.py:Verdict` is where it is defined. Four of them need a decision from you:

| Verdict | What to do |
|---|---|
| `settled` | Exercise the thing you actually changed. The gate sees the workload, not your change. |
| `needs-manual-apply` | **Read every remediation line, not the first.** One PR can reach two, and each prints its own. |
| `deferred` | Nothing is wrong; the next tick applies it. Re-run only if the line says this run stopped watching a tick mid-apply. |
| `tip-outran-retries` | A resume point, not a fault in your change: master merged faster than one tick-and-deploy cycle. Re-run the same command. |

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
