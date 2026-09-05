# `setup/claude_code` — Claude Code install + the Remote Control host

Two things live here. The **install** (native installer, per-user, auto-updating) and
**`claude-rc.service`**, the Remote Control host that lets sessions be created from a phone.

Runs on daniel-box only, gated by `has_claude_code` in
`inventory/host_vars/daniel-box.yml`. Invoked from `initial_setup.yml`, **not** `deploy.yml`
— the role is not in `containers_list`, so `./scripts/deploy.sh --tags claude_code` exits 2
on an unmatched tag. Deploy it with:

```bash
uv run ansible-playbook ansible/initial_setup.yml --tags claude_code
```

## The two Remote Control modes are different features

- `/remote-control` **inside a running session** publishes that one session to your phone.
  A session you already started. `remoteControlAtStartup` in the user's Claude Code settings
  does this automatically for every terminal session on the host.
- `claude rc` **from a shell** is a persistent server. It pre-creates one session so there is
  somewhere to type immediately, and spawns the rest **on demand** from claude.ai/code or the
  mobile app, up to `--capacity`. This is what `claude-rc.service` supervises.

Only the second one lets a session be created from the phone. It still needs a host process
running: Claude Code 2.1.241 reports *"Service install is disabled in this version — the
daemon runs on demand and exits when the last client disconnects"*, so there is no built-in
always-on host to enable, and systemd supervises it instead.

## Activating it

`claude_code_rc_enabled: true` — the host is enabled and started. It shipped stopped until
the prerequisite Ansible cannot check was confirmed by hand on 2026-08-23: a session created
from the phone produced `.claude/worktrees/test-scratch` on branch `worktree-test-scratch`
and reached a prompt, with no workspace-trust dialog.

Setting the var back to `false` stops **and** disables the host and its restart timer. That
is the rollback, and `ansible/tests/setup/test_claude_rc_unit.py` pins that both directions stay
wired.

To re-run that check after a change that could affect it — a Claude Code upgrade, a different
spawn mode — start a host by hand and create a session from the phone:

```bash
claude rc --spawn=worktree --permission-mode auto --capacity 2
```

Create a **new** session from the phone, not the one already waiting. `--create-session-in-dir`
is on by default, so one session is pre-created in the host's own directory; using that
proves the connection works and never exercises the spawn path at all.

Confirm four things, because the first cannot be checked from the repo:

| Check | Why it decides something |
|---|---|
| A spawned worktree does not demand a workspace-trust dialog | Nobody can answer that dialog from a phone. If it appears, `claude_code_rc_spawn_mode: same-dir` is the fallback. |
| The branch lands as `worktree-<name>` under `.claude/worktrees/` | Determines whether `prune_worktrees.py` collects them or they accumulate. |
| The host does not fight terminal sessions for Remote Control | Every terminal session here also claims RC via `remoteControlAtStartup`. |
| A phone-spawned session gets real auto mode | Claude Code has a distinct headless classifier path. If auto degrades without a TTY, `claude_code_rc_permission_mode: default` is the fallback — answering prompts remotely is what Remote Control is for. |

**Stop any hand-run host before deploying.** The service and a manually started `claude rc`
are two hosts competing for the same account and directory; the by-hand one is for the check
only.

## What monitoring does and does not cover

`OnFailure=claude-rc-alert.service` pages Discord when the host **crashes**, reusing the
shared `gitops_deploy_discord_webhook` like `gitops-deploy-alert` and `renovate-notify-alert`.

The webhook is **not** in the unit. It reaches `claude-rc-alert.service` through
`EnvironmentFile=/etc/claude-rc/alert-webhook.env`, rendered by its own 0600 task. The unit
originally interpolated the value straight into `ExecStart` and relied on `mode: 0600` to
protect it; the mode is real and irrelevant, because systemd serves unit content over the
system bus. `systemctl show claude-rc-alert -p ExecStart` printed the whole webhook to any
local user with no sudo, while `cat` on the same file was `Permission denied` — verified live
in the 2026-08-24 review (M-1). The sibling roles were moved off that shape 79 minutes before
this role landed, and this role's header comment cited them as justification for keeping it.
`ansible/roles/setup/gitops_deploy/tests/test_systemd_unit_secrets.py` now walks every
`*.service.j2` in the repo rather than naming units, so the next role cannot inherit it.

The **webhook task** carries `no_log: true` because it renders the secret; the unit task no
longer needs it. `no_log` also hides an undefined-variable failure, so if
`gitops_deploy_discord_webhook` is ever out of scope — the secret is loaded by
`initial_setup.yml`'s `pre_tasks`, tagged `always` — that task fails opaquely rather than
naming the missing var. Check that first if it fails for no apparent reason.

**It cannot catch an expired login.** The host keeps running and systemd keeps reporting
`active` while every session fails, so no `OnFailure=` ever fires and `Restart=always` has
nothing to restart. Closing that needs a check asserting the host is *registered*, not that
the process is up — a Kuma push or a `monitor-bridge` check. **Not yet built**: the check has
to match a failure signature nobody has observed, so it waits until the host has run long
enough to produce one. Until then an expired login shows up as "my phone can't start
sessions" and nothing else.

## Traps already paid for

- **`--spawn=` must be passed explicitly.** Omitted, Claude Code builds a readline interface
  on stdin to ask which mode to use (guarded on `process.stdin.isTTY`). Under systemd there
  is no stdin, so the host hangs having never connected while the unit reads `active`.
  ENFORCED by `ansible/tests/setup/test_claude_rc_unit.py`.
- **`PATH` must name `/usr/local/bin`.** systemd gives a minimal PATH exactly as cron does;
  without it a spawned session loses `kubectl` and `uv` and reports an **empty cluster**
  rather than failing. ENFORCED by the same test.
- **No `MemoryMax`.** systemd applies it to the whole cgroup, so one runaway session takes
  the OOM kill for the host and every other session, and `Restart=always` then returns an
  empty host. ENFORCED by the same test — which asserts the directive is absent, and says
  nothing about what bounds memory instead.
- **`claude_code_rc_capacity` does not bound memory.** It bounds session COUNT. The memory
  belongs to what a session spawns, and nothing about a session's size follows from there
  being ten of them. On 2026-09-05 two live sessions put 203 processes in the cgroup holding
  6.96 GB anon and all 8 GB of the system's swap; the cgroup then stalled in reclaim 98.5% of
  the time and remote control was unreachable for ~30 minutes while the unit read `active
  (running)`. `MemoryHigh` did not prevent it — it throttles rather than caps, so the cgroup
  kept allocating into swap past the threshold. Read `claude_code_rc_pytest_workers` for the
  bound that does apply, and issue #1154 for the swap-bound question this does not settle.
- **`claude_code_rc_pytest_workers` bounds the pytest fan-out**, via
  `PYTEST_XDIST_AUTO_NUM_WORKERS` in the unit. `addopts` in `pyproject.toml` carries
  `-n auto`; xdist reads this variable before any CPU detection, so a session's run gets 4
  workers rather than one per core. It caps a single run, NOT how many runs a session starts
  — nine concurrent runs is still nine times the cap, and a run-count limit would need a lock
  across worktrees. Setting it on the unit rather than in `pyproject.toml` keeps CI and an
  interactive shell at full width. ENFORCED by the same test.
- **The background-shell pressure reaper is turned off.** Claude Code registers
  `process.on("memoryPressure", ...)` and kills every running backgrounded Bash task with the
  reason `memory_pressure`, surfacing as "stopped because the system is running low on
  memory". The handler reads no threshold — it is a pass-through on Node's event, and Node
  derives that event from the **cgroup**, so `free -m` on the host measures the wrong scope
  and reads clean. The kill arrives as a task notification rather than an error in the
  logfile, so a `land.sh --arm-merge` reaped between the arm and the CI wait leaves the PR
  merged and undeployed with the session reading clean. `claude_code_rc_disable_bg_shell_pressure_reap`
  drives it both ways. Three kills in a row on 2026-09-04, issue #1096. ENFORCED by the same
  test.
- **The weekly restart uses `try-restart`.** Plain `restart` would start a host that
  `claude_code_rc_enabled` deliberately keeps stopped. It exists because Claude Code updates
  its binary in the background while a long-lived process keeps the version it started with.
  `RuntimeMaxSec=` was rejected for this: systemd records its expiry as a failure, which
  would page Discord weekly on a healthy host.
