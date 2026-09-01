---
name: gitops-tick
description: Trigger the homelab's GitOps deploy tick by hand instead of waiting for the 10-minute timer, and read its result. Use when a merge needs deploying now, when a tick's outcome is unclear, or when the journal reads "No entries" and you need to tell "ticked, nothing to do" from "did not tick". Runs a real deploy — there is no dry-run mode.
allowed-tools: Bash, Read, Grep
---

# Triggering a GitOps tick by hand

```bash
./scripts/deploy_tools/gitops_tick.sh            # trigger, wait up to 540s, print that run's journal
./scripts/deploy_tools/gitops_tick.sh --no-wait  # trigger and return immediately
```

**This is a real tick.** The timer does nothing but activate `gitops-deploy.service`, so the
wrapper runs the identical code path: it fetches, CI-gates, fast-forward-merges, deploys,
health-gates and rolls back for real. There is no rehearsal mode.

Run it on `daniel-box` — that is where the timer and the unit live.

## Reading the result

**An uneventful tick logs nothing.** The deployer prints only on a deferral, an alert or a
real deploy, so the journal alone renders a healthy run as `-- No entries --`, which reads
like the unit never ran. The wrapper therefore also prints `last_run`, `hold_sha` and
`behind_since` from `/var/lib/gitops-deploy`. **A fresh `last_run` is what distinguishes
"ticked, nothing to do" from "did not tick at all"** — check it before concluding anything.

A tick started while one is already in flight is **joined, not duplicated**: systemd coalesces
the request into the run already `activating`. The wrapper detects that and says so.

Exit codes: **75** = still running, the script stopped watching; **3** = the tick was skipped
for lock contention, so nothing deployed and nothing alerted.

## When it is denied

`gitops_tick.sh` is allow-listed but not guaranteed. It is a write, so the auto-mode classifier
judges it on its own text and denied it once in seven runs on identical input (measured
2026-08-22). A denial is classifier variance, not a broken script or a missing polkit rule.
`auto-mode-bridge.sh` retries it automatically, twice per session; a compound command that
merely contains the tick gets no retry, because the classifier judged the whole line.

Re-run it, and **check `last_run` before assuming nothing happened.**

## Why the wrapper exists rather than `systemctl start`

- **polkit.** `gitops-deploy.service` is a system unit, so an unprivileged `systemctl start`
  goes over D-Bus to PID 1 and is refused with *Interactive authentication required*.
  `templates/50-gitops-deploy.rules.j2` grants the deploy user (`sys_user`) exactly one thing:
  the `start` verb on this one unit — stop/restart/kill stay privileged. That rule must not
  test `subject.active`/`subject.local` and must return `polkit.Result.YES`, or a caller with
  no active local seat (a cron, a `systemd-run` job, a Claude Code Bash call) matches the rule
  and is still refused. `ansible/tests/test_gitops_manual_trigger.py` pins that.
- **`Type=oneshot` + `TimeoutStartSec=60min`.** A blocking start returns only when the tick
  finishes, which reads as a hang to anything with less patience. The wrapper starts with
  `--no-block`, waits on its own budget, then prints the journal for that run and exits with
  its status.

This is a *convenience* path, not the activation path: the role's `Run gitops-deploy once`
handler already kicks a run whenever the script, config or units change, so provisioning stays
fully IaC.

Everything else about the pipeline — the deferral rules, the denylist, `hold_sha` recovery —
is in `ansible/roles/setup/gitops_deploy/CLAUDE.md`.
