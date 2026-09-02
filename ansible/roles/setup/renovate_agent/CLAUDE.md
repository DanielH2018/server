# `setup/renovate_agent` — the unattended Renovate agent

A daily systemd timer that spends one headless Claude Code session
(`claude -p "/renovate-prs"`) on the repo's open Renovate PRs, then posts a Discord digest.
It is the acting half of the pair whose reporting half is `setup/renovate_notify`: that role
says what is open and what needs manual work, this one does it.

Runs on `renovate_agent_host` (`inventory/group_vars/all.yml`, daniel-box). Invoked from
`initial_setup.yml`, **not** `deploy.yml` — the role is not in `containers_list`, so
`./scripts/deploy.sh --tags renovate_agent` exits 2 on an unmatched tag. Deploy it with:

```bash
uv run ansible-playbook ansible/initial_setup.yml --tags renovate_agent
```

## Arming it

The role installs the script, config, prompt and units on every run.
`renovate_agent_enabled` alone decides whether the timer is enabled and started, and setting
it back to `false` stops **and** disables the timer. That is the rollback, and
`ansible/tests/setup/test_renovate_agent_unit.py` pins that both directions stay wired.

It ships `false`. Arming it is a decision with a spend attached and a blast radius: the
session merges PRs and lands them through `land.sh`, which deploys. Nothing about the role is
unfinished.

**`--check` fails at "Enable and start the timer", and that is not a bug in the role.** Check
mode writes no unit file, so systemd is then asked about a `renovate-agent.timer` that does
not exist and reports `Could not find the requested service`. Every task before it reports
correctly, which is what a check run of this role is actually good for. The sibling
`renovate_notify` role behaves the same way.

There is deliberately **no run-once handler**, where `renovate_notify` has one. Its run is a
read-only API query; this one costs money and changes the fleet, so a config edit must not
kick a session as a side effect. Start one by hand:

```bash
sudo systemctl start renovate-agent && journalctl -u renovate-agent -f
```

## Exercising the wrapper without arming anything

`RENOVATE_AGENT_CONFIG` overrides the config path, so the I/O shell can be run end-to-end
against a throwaway config before the timer exists. Point `REPO` at the real repo with the
backlog empty and the gate returns before it spends a session, which makes it a free pass
through config parsing, the `gh` census and the return path:

```bash
RENOVATE_AGENT_CONFIG=/tmp/agent-test.env \
  uv run python ansible/roles/setup/renovate_agent/files/renovate_agent.py
```

The override exists for exactly this. Without it the first armed tick would be the first time
this code ever ran.

## What bounds the run

The unit is deliberately **not** sandboxed, unlike the sibling `renovate-notify.service`.
This session drives git, gh, uv, ansible and kubectl and needs `~/.claude` for its own
credentials, so `ProtectHome`/`ProtectSystem` would have to be widened until they meant
nothing. Four other things bound it instead, and each is a var in `defaults/main.yml`:

| Bound | Var | Why that one |
|---|---|---|
| PRs per tick | `renovate_agent_max_prs` (3) | Each landing is a CI wait plus a tick plus a deploy plus a health gate. Three fits the wall clock; the rest wait for tomorrow. |
| Wall clock | `renovate_agent_run_timeout_s` (5400) | The wrapper's own kill, which still posts a digest naming the timeout. |
| Wall clock, backstop | `renovate_agent_unit_timeout` (100min) | systemd's. It kills the whole cgroup and posts only the OnFailure alert, so it must never trip first. |
| Spend | `renovate_agent_budget_usd` (25) | A runaway backstop, not a planned stop — see below. |

**The budget must not be the binding constraint.** Stopping a session mid-landing leaves a
merged-but-undeployed change, which is exactly what the root `CLAUDE.md`'s post-merge section
forbids. The PR cap is what bounds a normal run; the budget only catches a loop.

**The session never runs in the primary checkout.** One untracked file in
`/home/<user>/server` parks the GitOps deployer silently, and a session that edits, renders
and tests is guaranteed to leave some. Each tick recreates
`.claude/worktrees/renovate-auto` at `origin/master` and runs there. `land.sh` cds to the
primary checkout itself, so the landing steps still deploy the right tree.

**A worktree holding work is not thrown away.** If the previous tick left uncommitted changes
or commits that never reached `origin/master`, the tick skips and posts the path. Removing it
is how unlanded work is lost.

## The digest measures effect, not completion

`is_error: false` plus `terminal_reason: completed` means the process ended cleanly, not that
any PR moved — a session that achieved nothing still writes a confident closing paragraph.
So the wrapper censuses `gh pr list --author app/renovate --state open` **before and after**,
and the digest's headline is that delta:

- `✅ resolved #a, #b` — those PR numbers left the open set.
- `⚠️ ran and no Renovate PR changed state` — the failure that would otherwise read green.
- `🚨 FAILED — <reason>` — timeout, non-zero exit, or `is_error`.

`permission denials:` on a digest line is the one to act on. Headless auto mode approving the
session's writes is the assumption the whole design rests on; it was verified on 2026-09-02
against Claude Code 2.1.258 (a bash file create, a `sed -i`, and an `Edit` tool call all
landed with `permission_denials: []`), but a Claude Code upgrade can change it, and the
failure mode is a session that reads green and does nothing.

## Not yet built

**No alive monitor.** `renovate_agent_kuma_push_token` is unset, so the unit renders no
`ExecStartPost` beat and a timer that stops firing is invisible until someone notices the
backlog. Closing it needs a push monitor created in Uptime Kuma first and its token added to
SOPS — inventing a token here would produce a monitor that has never existed, which is worse
than none.
