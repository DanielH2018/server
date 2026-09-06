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
  bound on the fan-out, and `claude_code_rc_memory_swap_max` for the ceiling on the swap.
- **`MemorySwapMax` caps the cgroup's swap, and `MemoryHigh` cannot.** A throttled cgroup's
  anon pages go to swap, so an unbounded `memory.swap.max` left the throttle with no ceiling
  — the mechanism behind the 2026-09-05 stall, and what issue #1154 asked for.
  `claude_code_rc_memory_swap_max` sets one plane's ceiling to 2G, and
  `claude_code_fleet_swap_max` holds the two planes together at the same 2G — a quarter of
  this host's 8 GiB `/swap.img`, so the homelab plane keeps three quarters. Until the fleet
  bound landed (#1264) the per-plane 2G was applied twice and the fleet's real ceiling was
  4G. **Not 0:** with no swap outlet nothing
  reclaims the cgroup's anon pages and, with `MemoryMax` deliberately infinity, the terminal
  state becomes the global OOM killer choosing a victim by badness anywhere on the box. The
  cap converts theft of the whole box's swap into a bounded share plus harder reclaim
  throttling; it does not make the cgroup safe. ENFORCED by
  `ansible/tests/setup/test_claude_rc_unit.py`.
- **`claude_code_rc_pytest_workers` bounds the pytest fan-out**, via
  `PYTEST_XDIST_AUTO_NUM_WORKERS` in the unit. `addopts` in `pyproject.toml` carries
  `-n auto`; xdist reads this variable before any CPU detection, so a session's run gets 4
  workers rather than one per core. It caps a single run, NOT how many runs a session starts
  — nine concurrent runs is still nine times the cap, and a run-count limit would need a lock
  across worktrees. Setting it on the unit rather than in `pyproject.toml` keeps CI and an
  interactive shell at full width. ENFORCED by the same test.
- **The unit is not the only place this variable is set.** `PYTEST_XDIST_AUTO_NUM_WORKERS`
  also sits in the `env` block of `~/.claude/settings.json`, scoped to daniel-box and
  daniel-server, so a Claude session started outside this unit — a terminal, a background
  job, an IDE — gets the same cap. That entry is generated from
  `home/.chezmoitemplates/settings.base.json` in the chezmoi repo, not from this role. Keep
  the two values equal: if they diverge, a session's fan-out depends on how it happened to
  start. A human's own `pytest` in their own shell, and CI, stay at full width either way —
  neither reads Claude's settings.
- **Neither of those two covers a session started with `claude agents` from an interactive
  SSH shell.** That process lands in
  `user.slice/user-{{ claude_code_login_uid }}.slice/session-<n>.scope`, which reads none of
  the unit's `Environment=`/`MemoryHigh=`/`MemorySwapMax=` lines and, depending on how the
  shell was reached, may not load `~/.claude/settings.json` either — issue #1213 measured
  965 processes and 20.4 GB anon in exactly that scope on 2026-09-05 while claude-rc.service
  held 50.9 MB. Two more artifacts close the gap, both rendered by this role and driven by
  the same variables as the unit rather than a second hardcoded number:
  `templates/login-slice-caps.conf.j2` is a systemd drop-in on `user-<uid>.slice` carrying
  the same `MemoryHigh`/`MemorySwapMax` as the unit (`claude_code_login_uid` names the uid;
  default `1000`, confirmed by the incident's own scope path), and
  `templates/pytest-fanout-cap.conf.j2` is a `~/.config/environment.d/` file carrying the
  same `PYTEST_XDIST_AUTO_NUM_WORKERS`, read into the session's PAM environment by
  `pam_systemd` at login. `claude_code_login_caps_enabled` (default `true`) turns both off
  and removes the rendered files, if the cap on a login session ever needs to come off.
  Effect timing differs from the unit: the slice drop-in applies live to an already-running
  session on `daemon-reload`, but the environment.d file only takes effect on the *next*
  login. ENFORCED by `ansible/tests/setup/test_claude_login_slice_caps.py`.
- **Sharing a variable is not sharing a cap.** The two artifacts above render
  `claude_code_rc_memory_high` at two render sites, which reads as one 8G bound and was two:
  `claude-rc.service` and `user-<uid>.slice` are cgroup siblings under *different* parents
  (`system.slice` and `user.slice`), so neither saw the other's usage and the fleet's real
  throttle point was the SUM — 16G of anon plus 4G of swap on a box with 28 GiB of RAM.
  Issue #1264. Closed by putting one cap on the slice that parents both planes; the section
  below is the answer to the question that fix had to settle first.
  ENFORCED by `ansible/tests/setup/test_claude_fleet_slice_cap.py`.

## The fleet bound, and why it lives on `user.slice`

One number for both Claude cgroups, on the one slice that is a parent of both:
`claude_code_fleet_memory_high` / `claude_code_fleet_swap_max`, rendered by
`templates/fleet-slice-caps.conf.j2` into
`/etc/systemd/system/user.slice.d/claude-fleet-caps.conf`. `claude-rc.service` reaches that
parent through a `Slice=` line; `user-<uid>.slice` is under `user.slice` already. The
per-plane 8G/2G stay as sub-bounds, so either plane may take most of the budget while the
other is idle and the parent is what holds the pair. `claude_code_fleet_caps_enabled: false`
removes the drop-in and the `Slice=` line together, returning the unit to `system.slice`.

**`user-<uid>.slice` cannot be reparented, and that is what picked the shape.** A slice's
parent is its *name*: systemd.slice(5) says "The name of the slice encodes the location in
the tree", and for a slice unit `Slice=` accepts "the only accepted value ... the parent
slice". `systemd-logind` creates the slice under that name, so nothing in this role can move
it. The unit this role *does* control is the one that moves instead.

**Nesting the RC unit one level deeper — inside `user-<uid>.slice` — would also share a
parent, and is unsafe here.** That slice is `StopWhenUnneeded=yes`
(`/usr/lib/systemd/system/user-.slice.d/10-defaults.conf`) and this host has `Linger=no`
(`loginctl show-user ubuntu`), so logind stops it at the last logout — and a unit with
`Slice=` gains an implicit `Requires=` on its slice (systemd.resource-control(5)), so the RC
host would be stopped with it and `Restart=always` does not bring back a dependency-stopped
unit. `user.slice` measures `StopWhenUnneeded=no`.

**A system service in a user-tree slice is systemd's own pattern**, not a workaround:
`user@.service` and `user-runtime-dir@.service` both ship `Slice=user-%i.slice`, and
systemd.special(7) describes `user.slice` as holding "all user processes and services started
on behalf of the user" — which is what this unit is (`User=ubuntu`, `HOME=/home/ubuntu`, a
per-user install reading that user's own OAuth token).

**The derivation lives in `defaults/main.yml`** beside `claude_code_fleet_memory_high` —
28.2 GiB of RAM, a 10 GiB budget for the homelab plane's anon, `SUnreclaim` and a page-cache
floor, against a measured 24h fleet peak of 11.2 GiB. Re-derive it there, not from this page.
Note that `memory.high` counts page cache, so the fleet touches this bound during an ordinary
multi-agent pytest fan-out and reclaims cache; the anon budget is what the number is derived
from, not what it throttles on.

**Two effect-timing facts for the deploy.** The parent drop-in applies live on
`daemon-reload`, so it lands on already-running login sessions. The `Slice=` line does not
migrate a running unit — it takes effect at the next start, so the deploy that changes it
restarts the RC host and drops the sessions it had spawned. Verify from cgroupfs rather than
from `systemctl show`, which reports the configured slice before the restart has moved the
processes:

```bash
systemctl show -p Slice -p MemoryHigh -p MemorySwapMax claude-rc.service user.slice user-1000.slice
cat /sys/fs/cgroup/user.slice/memory.high /sys/fs/cgroup/user.slice/memory.swap.max
ls -d /sys/fs/cgroup/user.slice/claude-rc.service
```
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
