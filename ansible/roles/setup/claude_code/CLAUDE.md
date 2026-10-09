# `setup/claude_code` — Claude Code install + the Remote Control host

Three things live here: the **install** (native installer, per-user, auto-updating),
**`claude-rc.service`**, the Remote Control host that lets sessions be created from a phone, and
**`claude-memory-sync.timer`**, which copies daniel-box's Claude memory store to daniel-server.

Runs on every host with `has_claude_code: true` — daniel-box and daniel-server, each with its own
cap numbers in host_vars. The unit is enabled only where `claude_code_rc_enabled` is also
true (daniel-box). Invoked from `initial_setup.yml`, **not** `deploy.yml` — the role is not in
`containers_list`, so `./scripts/deploy.sh --tags claude_code` exits 2 on an unmatched tag:

```bash
uv run ansible-playbook ansible/initial_setup.yml --tags claude_code
```

`docs/claude-code-rc-caps.md` holds the record behind the rules below: the memory incidents,
the fleet bound, the phone check, the webhook, the restart design and cgroupfs checks.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's tasks, timer templates, defaults or playbook entry, or a schedule var in group_vars/all.yml. -->
- **Applied by:** `initial_setup.yml --tags "claude_code"` when `has_claude_code`
- **Timers (3):** `claude-cgroup-metrics.timer` (`OnBootSec=30s`, `OnUnitActiveSec=30s`),
  `claude-memory-sync.timer` (`OnBootSec=5min`, `OnUnitActiveSec=15min`),
  `claude-rc-restart.timer` (`OnCalendar=weekly`)
<!-- /generated_from -->

## The two Remote Control modes are different features

`/remote-control` **inside a running session** publishes that one session to the phone. `claude rc`
**from a shell** is a persistent server that spawns sessions **on demand** up to `--capacity`; only
it lets the phone create a session, and Claude Code ships no always-on host, so systemd runs one.

## Activating it

`claude_code_rc_enabled: true` enables and starts the host; back to `false` stops **and** disables
it and its restart timer, which is the rollback. `ansible/tests/setup/test_claude_rc_unit.py` pins
that both directions stay wired.

One prerequisite Ansible cannot check: a phone-created session reaches a prompt in a fresh
worktree with no workspace-trust dialog. **Re-run that check after a Claude Code upgrade or a
spawn-mode change.**

**Stop any hand-run host before deploying** — the service and a manual `claude rc` compete for one
account and directory.

## What monitoring does and does not cover

`OnFailure=claude-rc-alert.service` pages Discord when the host **crashes**, reusing the shared
`gitops_deploy_discord_webhook` like `gitops-deploy-alert` and `renovate-notify-alert`.

- **That value reaches the alert unit through an `EnvironmentFile=`, never the unit body.**
  `ansible/roles/setup/gitops_deploy/tests/test_systemd_unit_secrets.py` holds the whole repo to
  that shape. `no_log: true` sits on the rendering task only and also hides an undefined-variable
  failure — check `gitops_deploy_discord_webhook` is in scope if that task fails opaquely.
- **It cannot catch an expired login.** The host keeps reporting `active` while every session
  fails, so no `OnFailure=` fires. Closing it needs a check that the host is *registered*, not
  just up — **not yet built**.

## Traps already paid for

`ansible/tests/setup/test_claude_rc_unit.py` enforces every unit-shape rule here;
`ansible/tests/setup/test_claude_login_slice_caps.py` and
`ansible/tests/setup/test_claude_fleet_slice_cap.py` enforce the two slice planes.

- **`--spawn=` must be passed explicitly**, or Claude Code asks on stdin which mode to use and the
  host hangs under systemd, never connected, while the unit reads `active`.
- **`PATH` must name `/usr/local/bin`**, as with cron: without it a spawned session loses `kubectl`
  and `uv` and reports an **empty cluster** rather than failing.
- **No `MemoryMax`**, which systemd applies to the whole cgroup: one runaway session would take the
  OOM kill for every other session too.
- **`claude_code_rc_capacity` bounds session count, not memory**, and `MemoryHigh` throttles rather
  than caps. `claude_code_rc_memory_swap_max` (2G) is the ceiling, and **0 is wrong**.
- **`claude_code_rc_pytest_workers` caps one pytest run's fan-out, not how many runs a session
  starts**, via `PYTEST_XDIST_AUTO_NUM_WORKERS` in the unit. **The same variable also sits in
  `~/.claude/settings.json`**, generated from the chezmoi repo — keep the two equal.
- **A session started with `claude agents` from an interactive SSH shell reads none of the unit's
  directives**: it lands in `user.slice/user-{{ claude_code_login_uid }}.slice/session-<n>.scope`.
  `templates/login-slice-caps.conf.j2` and `templates/pytest-fanout-cap.conf.j2` carry the unit's caps
  there, and `claude_code_login_caps_enabled` (default `true`) removes both.
- **The background-shell pressure reaper is turned off.**
  `claude_code_rc_disable_bg_shell_pressure_reap` drives it both ways: Node derives the kill from
  the **cgroup**, so `free -m` reads clean while it kills backgrounded Bash tasks.
- **The weekly restart uses `try-restart`**, because plain `restart` would start a host that
  `claude_code_rc_enabled` deliberately keeps stopped. It exists to pick up the binary Claude Code
  updates in the background, which a long-lived process otherwise never does.

## The fleet bound, and why it lives on `user.slice`

One number for both Claude cgroups, on the one slice that parents both:
`claude_code_fleet_memory_high` / `claude_code_fleet_swap_max`, rendered by
`templates/fleet-slice-caps.conf.j2`. `claude-rc.service` reaches that parent through a `Slice=`
line, and `claude_code_fleet_caps_enabled: false` removes both.

**Re-derive the number in `defaults/main.yml`**, never from a summary. **The `Slice=` line takes
effect at the next start**, so the deploy that changes it drops the RC host's sessions.

## The agent user

`claude_code_agent_user_enabled` builds `claude` with
`ansible/roles/setup/common/tasks/agent_user.yml`. No unit runs as it. It lands a PR by starting
`claude-land@<n>.service`, which runs `land.sh` as the operator under the landing policy.
`defaults/main.yml` covers login, the GitHub account, the lander and switching each off.

## Autonomous-role contract (`claude-memory-sync` overwrites a store on another host)

- **Scope:** `rsync --delete` of `claude_code_memory_sync_dir` to the same path on
  `claude_code_memory_sync_target`, one-way. A memory a daniel-server session writes is lost
  at the next run, by the operator's decision (#3123); `docs/claude-memory-sync.md` has why.
- **Mode:** `claude_code_memory_sync_enabled`, true only in daniel-box's host_vars; false
  stops the timer and removes its units.
- **Abort valve:** the unit skips unless the source `MEMORY.md` is non-empty.
- **Evidence:** `journalctl -u claude-memory-sync` lists each file a run changed or deleted;
  a failure pages Discord.
