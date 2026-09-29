# `setup/claude_code` — Claude Code install + the Remote Control host

Two things live here: the **install** (native installer, per-user, auto-updating) and
**`claude-rc.service`**, the Remote Control host that lets sessions be created from a phone.

Runs on every host with `has_claude_code: true` — daniel-box and daniel-server, each carrying its own
cap numbers in its own host_vars. The unit is enabled only where `claude_code_rc_enabled` is also
true (daniel-box). Invoked from `initial_setup.yml`, **not** `deploy.yml` — the role is not in
`containers_list`, so `./scripts/deploy.sh --tags claude_code` exits 2 on an unmatched tag:

```bash
uv run ansible-playbook ansible/initial_setup.yml --tags claude_code
```

`docs/claude-code-rc-caps.md` holds the record behind the rules below — the three memory incidents,
the argument that put the fleet bound on `user.slice`, the phone check's own procedure, and how to
verify a caps deploy from cgroupfs.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's tasks, timer templates, defaults or playbook entry, or a schedule var in group_vars/all.yml. -->
- **Applied by:** `initial_setup.yml --tags "claude_code"` when `has_claude_code`
- **Timers (2):** `claude-cgroup-metrics.timer` (`OnBootSec=30s`, `OnUnitActiveSec=30s`),
  `claude-rc-restart.timer` (`OnCalendar=weekly`)
<!-- /generated_from -->

## The two Remote Control modes are different features

- `/remote-control` **inside a running session** publishes that one session to your phone, as
  `remoteControlAtStartup` does for every terminal session here.
- `claude rc` **from a shell** is a persistent server: one pre-created session, the rest spawned
  **on demand** up to `--capacity`. `claude-rc.service` supervises this one, and only this one lets a
  session be created from the phone.

Claude Code ships no always-on host to enable, which is why systemd supervises one.

## Activating it

`claude_code_rc_enabled: true` enables and starts the host; back to `false` stops **and** disables
it and its restart timer, which is the rollback. `ansible/tests/setup/test_claude_rc_unit.py` pins
that both directions stay wired.

One prerequisite Ansible cannot check, confirmed by hand on 2026-08-23: a phone-created session
reaches a prompt in a fresh worktree with no workspace-trust dialog. **Re-run that check after a
Claude Code upgrade or a spawn-mode change** — the docs page has the command, the four things to
confirm and each fallback.

**Stop any hand-run host before deploying** — the service and a manual `claude rc` compete for the
same account and directory.

## What monitoring does and does not cover

`OnFailure=claude-rc-alert.service` pages Discord when the host **crashes**, reusing the shared
`gitops_deploy_discord_webhook` like `gitops-deploy-alert` and `renovate-notify-alert`.

- **That value reaches the alert unit through an `EnvironmentFile=`, never the unit body**, because
  systemd serves unit content over the system bus to any local user;
  `ansible/roles/setup/gitops_deploy/tests/test_systemd_unit_secrets.py` holds the whole repo to that
  shape. `no_log: true` sits on the rendering task only, and it hides an undefined-variable failure
  too — check `gitops_deploy_discord_webhook` is in scope if that task fails opaquely.
- **It cannot catch an expired login.** The host keeps running and systemd keeps reporting `active`
  while every session fails, so no `OnFailure=` fires. Closing it needs a check asserting the host is
  *registered* rather than up — **not yet built**, because no failure signature has been observed.

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
  than caps. `claude_code_rc_memory_swap_max` (2G) is the ceiling, and **0 is wrong** — with no swap
  outlet the terminal state is the global OOM killer.
- **`claude_code_rc_pytest_workers` caps one pytest run's fan-out, not how many runs a session
  starts**, via `PYTEST_XDIST_AUTO_NUM_WORKERS` in the unit. **The same variable also sits in
  `~/.claude/settings.json`**, generated from the chezmoi repo — keep the two equal.
- **A session started with `claude agents` from an interactive SSH shell reads none of the unit's
  directives**: it lands in `user.slice/user-{{ claude_code_login_uid }}.slice/session-<n>.scope`.
  `templates/login-slice-caps.conf.j2` and `templates/pytest-fanout-cap.conf.j2` carry the unit's caps
  there, and `claude_code_login_caps_enabled` (default `true`) removes both.
- **The background-shell pressure reaper is turned off** (#1096): Node derives `memoryPressure` from
  the **cgroup**, so `free -m` reads clean while backgrounded Bash tasks die, and the kill arrives as
  a task notification rather than a logfile error.
  `claude_code_rc_disable_bg_shell_pressure_reap` drives it both ways.
- **The weekly restart uses `try-restart`**, because plain `restart` would start a host that
  `claude_code_rc_enabled` deliberately keeps stopped. It exists because Claude Code updates its
  binary in the background while a long-lived process keeps the version it started with, and
  `RuntimeMaxSec=` was rejected: systemd records its expiry as a failure.

## The fleet bound, and why it lives on `user.slice`

One number for both Claude cgroups, on the one slice that parents both:
`claude_code_fleet_memory_high` / `claude_code_fleet_swap_max`, rendered by
`templates/fleet-slice-caps.conf.j2`. `claude-rc.service` reaches that parent through a `Slice=`
line, the per-plane caps remain as sub-bounds, and `claude_code_fleet_caps_enabled: false` removes
both.

**`user-<uid>.slice` cannot be reparented** — a slice's parent is its name — which is why the RC unit
is what moves, and **sharing a variable is not sharing a cap**: rendered at two sibling cgroups under
different parents, one number bounds each plane separately and the fleet's throttle point is the sum
(#1264). **Re-derive the number in `defaults/main.yml`**, never from a summary. **The `Slice=` line
takes effect at the next start**, so the deploy that changes it drops the sessions the RC host had
spawned; verify from cgroupfs rather than `systemctl show`, which the docs page covers.
