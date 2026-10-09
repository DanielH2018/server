# `setup/claude_code` — Claude Code install + the Remote Control host

Three things live here: the **install** (native installer, per-user, auto-updating),
**`claude-rc.service`**, the Remote Control host that lets sessions be created from a phone, and
**`claude-memory-sync.timer`**, which copies daniel-box's Claude memory store to daniel-server.

Runs on every host with `has_claude_code: true` (daniel-box and daniel-server, with per-host cap
numbers in host_vars). The unit is enabled only where `claude_code_rc_enabled` is also true
(daniel-box). Invoked from `initial_setup.yml`, **not** `deploy.yml` — the role is not in
`containers_list`, so `./scripts/deploy.sh --tags claude_code` exits 2 on an unmatched tag:

```bash
uv run ansible-playbook ansible/initial_setup.yml --tags claude_code
```

`docs/claude-code-rc-caps.md` holds the record behind the rules below.

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
it lets the phone create a session, so systemd runs one.

## Activating it

`claude_code_rc_enabled: true` enables and starts the host; back to `false` stops **and** disables
it and its restart timer, which is the rollback. `ansible/tests/setup/test_claude_rc_unit.py` pins both directions.

One prerequisite Ansible cannot check: a phone-created session reaches a prompt in a fresh
worktree with no workspace-trust dialog. **Re-run that check after a Claude Code upgrade or
spawn-mode change.**

**Stop any hand-run host before deploying**: it and the service compete for one account and
directory.

## What monitoring does and does not cover

`OnFailure=claude-rc-alert.service` pages Discord when the host **crashes**, reusing the shared
`gitops_deploy_discord_webhook`.

- **That value reaches the alert unit through an `EnvironmentFile=`, never the unit body.**
  `ansible/roles/setup/gitops_deploy/tests/test_systemd_unit_secrets.py` holds the whole repo to
  that shape. `no_log: true` hides an undefined-variable failure, so when the task
  fails opaquely, check `gitops_deploy_discord_webhook` is in scope.
- **It cannot catch an expired login.** The host keeps reporting `active` while every session
  fails, so no `OnFailure=` fires. Closing it needs a check that the host is *registered*, not
  just up, and none exists.

## Traps already paid for

`ansible/tests/setup/test_claude_rc_unit.py` enforces every unit-shape rule here;
`ansible/tests/setup/test_claude_login_slice_caps.py` and
`ansible/tests/setup/test_claude_fleet_slice_cap.py` enforce the two slice planes.

- **`--spawn=` must be passed explicitly**, or Claude Code asks on stdin which mode to use and the
  host hangs under systemd, never connected, while the unit reads `active`.
- **`PATH` must name `/usr/local/bin`**, as with cron: without it a spawned session loses `kubectl`
  and `uv` and reports an **empty cluster** rather than failing.
- **No `MemoryMax`**: systemd applies it to the whole cgroup, so one runaway session takes the
  OOM kill for every session.
- **`claude_code_rc_capacity` bounds session count, not memory**, and `MemoryHigh` throttles rather
  than caps. `claude_code_rc_memory_swap_max` is the ceiling, and **0 is wrong**.
- **`claude_code_rc_pytest_workers` caps one pytest run's fan-out, not the number of runs**, via
  `PYTEST_XDIST_AUTO_NUM_WORKERS` in the unit. **`~/.claude/settings.json` carries the same
  variable**, generated from the chezmoi repo; keep the two equal.
- **A session started with `claude agents` reads none of the unit's directives**: it lands in
  `user-<uid>.slice`. `login-slice-caps.conf.j2` and `pytest-fanout-cap.conf.j2` carry
  the unit's caps there for each uid in `claude_code_login_uids` and the agent's. A uid that
  leaves the list loses both; `claude_code_login_caps_enabled: false` removes all.
- **The background-shell pressure reaper is turned off.**
  `claude_code_rc_disable_bg_shell_pressure_reap` drives it both ways: Node derives the kill from
  the **cgroup**, so `free -m` reads clean while it kills backgrounded Bash tasks.
- **The weekly restart uses `try-restart`**: plain `restart` would start a host that
  `claude_code_rc_enabled` keeps stopped on purpose. It picks up the binary Claude Code updates
  in the background.

## The fleet bound, and why it lives on `user.slice`

One number for both Claude cgroups, on the one slice that parents both:
`claude_code_fleet_memory_high` / `claude_code_fleet_swap_max`, rendered by
`templates/fleet-slice-caps.conf.j2`. `claude-rc.service` reaches that parent through a `Slice=`
line, and `claude_code_fleet_caps_enabled: false` removes both.

**Re-derive the number in `defaults/main.yml`**, never from a summary. **The `Slice=` line takes
effect at the next start**, so the deploy that changes it drops the RC host's sessions.

## The agent user

`claude_code_agent_user_enabled` builds `claude` with
`ansible/roles/setup/common/tasks/agent_user.yml`. The agent lands a PR by starting
`claude-land@<n>.service`, which runs `land.sh` as the operator under the landing policy.
`defaults/main.yml` covers login, the GitHub account, the lander and each switch.

`claude_code_user` (default `sys_user`) is the account `claude-rc.service` runs as, and its home
and `claude_code_rc_workdir` follow it. The unit gets `ProtectHome=yes`, `NoNewPrivileges=yes`,
`PrivateTmp=yes` and `UMask=0027` only when it differs from `sys_user`. The first apply as the
agent copies the operator's memory store once (`tasks/agent_memory_seed.yml`).

## Autonomous-role contract (`claude-memory-sync` overwrites a store on another host)

- **Scope:** `rsync --delete` of `claude_code_memory_sync_dir` (follows `claude_code_user`) to
  `claude_code_memory_sync_target_dir` (the operator's store)
  on `claude_code_memory_sync_target`, one-way. A memory a daniel-server session writes is lost
  at the next run, by the operator's decision (#3123); `docs/claude-memory-sync.md` has why.
- **Mode:** `claude_code_memory_sync_enabled`, true only in daniel-box's host_vars; false
  stops the timer and removes its units.
- **Abort valve:** the unit skips unless the source `MEMORY.md` is non-empty.
- **Evidence:** `journalctl -u claude-memory-sync` lists each file a run changed or deleted;
  a failure pages Discord.
