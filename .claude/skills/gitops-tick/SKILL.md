---
name: gitops-tick
description: Trigger the homelab's GitOps deploy tick by hand instead of waiting for the 10-minute timer, and read its result. Use when a setup-role merge needs applying now, when a tick's outcome is unclear, or when the journal reads "No entries" and you need to tell "ticked, nothing to do" from "did not tick". Runs a real deploy — there is no dry-run mode.
allowed-tools: Bash, Read, Grep
---

# Triggering a GitOps tick by hand

`land.sh` starts no tick and watches none (H7.49a). The deployer's own timer applies a setup
role, and this skill is the way to apply one sooner.

```bash
systemctl start --no-block gitops-deploy.service
cc-wait systemd gitops-deploy.service
uv run python scripts/diagnostics/probe.py gitops-state
```

Run it on `daniel-box` — that is where the timer and the unit live.

**This is a real tick.** The timer does nothing but activate `gitops-deploy.service`, so a hand
start runs the identical code path: it fetches, CI-gates, fast-forward-merges, applies the setup
roles it may and holds on a failure. There is no rehearsal mode.

## Reading the result

**An uneventful tick logs nothing.** The deployer prints only on a deferral, an alert or a real
apply, so the journal alone renders a healthy run as `-- No entries --`, which reads like the
unit never ran. `probe.py gitops-state` prints `last_run`, the hold with its planes,
`behind_since`, `contention_since` and the owed ledger, each with its clear command. A
`last_run` newer than your start is the line that says the tick happened at all. The journal is
`journalctl -u gitops-deploy.service`.

A start while a tick is already in flight is **joined, not duplicated**: systemd coalesces the
request into the run already `activating`. That run fetched origin before your request, so a
commit merged since is not in it. Start again once it ends.

## Why `systemctl start` is enough here

- **polkit.** `gitops-deploy.service` is a system unit, so an unprivileged `systemctl start`
  would go over D-Bus to PID 1 and be refused with *Interactive authentication required*.
  `templates/50-gitops-deploy.rules.j2` grants the deploy user (`sys_user`) exactly one thing:
  the `start` verb on this one unit — stop/restart/kill stay privileged. That rule must not
  test `subject.active`/`subject.local` and must return `polkit.Result.YES`, or a caller with
  no active local seat (a cron, a `systemd-run` job, a Claude Code Bash call) matches the rule
  and is still refused. `ansible/tests/deploy/test_gitops_manual_trigger.py` pins that.
- **`Type=oneshot` + `TimeoutStartSec=40min`.** A blocking start returns only when the tick
  finishes, which reads as a hang to anything with less patience. Pass `--no-block` and wait with
  `cc-wait systemd`.

This is a *convenience* path, not the activation path: the role's `Run gitops-deploy once`
handler already kicks a run whenever the script, config or units change, so provisioning stays
fully IaC.

Everything else about the pipeline — the deferral rules, the denylist, `hold_sha` recovery —
is in `ansible/roles/setup/gitops_deploy/CLAUDE.md`.
