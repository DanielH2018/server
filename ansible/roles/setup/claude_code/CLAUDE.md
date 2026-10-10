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
- **Timers (4):** `claude-cgroup-metrics.timer` (`OnBootSec=30s`, `OnUnitActiveSec=30s`),
  `claude-clone-sync.timer` (`OnBootSec=10min`, `OnUnitActiveSec=15min`),
  `claude-memory-sync.timer` (`OnBootSec=5min`, `OnUnitActiveSec=15min`),
  `claude-rc-restart.timer` (`OnCalendar=weekly`)
<!-- /generated_from -->

## The two Remote Control modes are different features

`/remote-control` **inside a session** publishes that session to the phone. `claude rc` **from a
shell** spawns sessions **on demand** up to `--capacity`, so only it lets the phone create one.

## Activating it

`claude_code_rc_enabled: true` enables and starts the host; back to `false` stops **and** disables
it and its restart timer, which is the rollback. `ansible/tests/setup/test_claude_rc_unit.py` pins both directions.

One prerequisite Ansible cannot check, the trust dialog, is in `docs/claude-code-rc-caps.md`. **Re-run that check after a Claude Code upgrade or spawn-mode change.**

**Stop any hand-run host before deploying**: it and the service compete for one account and
directory.

## What monitoring does and does not cover

`OnFailure=claude-rc-alert.service` pages Discord when the host **crashes**, reusing the shared
`gitops_deploy_discord_webhook`.

- **That value reaches the alert unit through an `EnvironmentFile=`, never the unit body.**
  `ansible/roles/setup/gitops_deploy/tests/test_systemd_unit_secrets.py` holds the whole repo to
  that shape. `no_log: true` hides an undefined-variable failure, so when the task
  fails opaquely, check `gitops_deploy_discord_webhook` is in scope.
- **It cannot catch an expired login.** The host reads `active` while every session fails, and
  no check reads whether it is *registered*.

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
  variable**: this role writes the agent's, so keep chezmoi's equal.
- **A session started with `claude agents` reads none of the unit's directives**: it lands in
  `user-<uid>.slice`. `login-slice-caps.conf.j2` and `pytest-fanout-cap.conf.j2` carry
  the unit's caps there for each uid in `claude_code_login_uids` and the agent's. A uid that
  leaves the list loses both; `claude_code_login_caps_enabled: false` removes all.
- **The background-shell pressure reaper is off** (`claude_code_rc_disable_bg_shell_pressure_reap`):
  Node reads the **cgroup**, so `free -m` reads clean while it kills backgrounded Bash tasks.
- **The weekly restart uses `try-restart`**, which never starts a host kept stopped on purpose.

## The fleet bound, and why it lives on `user.slice`

`claude_code_fleet_memory_high` / `claude_code_fleet_swap_max` bound both Claude cgroups on
`user.slice`, their shared parent; `claude_code_fleet_caps_enabled: false` removes both.
`docs/claude-code-rc-caps.md` has the derivation. **The `Slice=` line takes effect at the next
start**, so the deploy that changes it drops the RC host's sessions.

## The agent user

`tasks/agent.yml` builds one agent user per `claude_code_agents` entry; the include maps each
profile field (`ansible/filter_plugins/claude_agents.py`) onto the `claude_code_agent_*` name
the tasks read. `docs/claude-agent-user.md` has the rest. `claude` lands a PR through
`claude-land@<n>.service`.

`claude_code_user` (default `sys_user`) is the account `claude-rc.service` runs as, and its home
and `claude_code_rc_workdir` follow it. The unit is sandboxed (`ProtectHome=yes` and three more)
only when it differs from `sys_user`. The first apply as the
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

## Autonomous-role contract (`claude-clone-sync`)

- **Scope:** a `--ff-only` pull of each agent's clone every
  `claude_code_agent_clone_sync_interval`, then the venv and collections if their locks moved.
- **Mode:** the agent's `state`; absent removes its `<name>-clone-sync` units.
- **Abort valve:** it skips, exiting 0, unless the clone is a clean `master`.
- **Evidence:** `journalctl -u <name>-clone-sync`; a failure pages Discord.
