# claude-rc caps — the derivations behind the memory bounds

The role doc `ansible/roles/setup/claude_code/CLAUDE.md` keeps the rules. This page keeps the
live phone check, the arguments that settled which slice carries the fleet bound, and the
reasoning behind a few unit-shape rules.

## The phone check Ansible cannot run

Start a host by hand and create a session from the phone:

```bash
claude rc --spawn=worktree --permission-mode auto --capacity 2
```

Create a **new** session from the phone, not the one already waiting. `--create-session-in-dir` is on
by default, so one session is pre-created in the host's own directory; using that proves the
connection works and never exercises the spawn path at all. Then confirm four things, the first of
which cannot be checked from the repo:

| Check | Why it decides something |
|---|---|
| A spawned worktree does not demand a workspace-trust dialog | Nobody can answer that dialog from a phone. If it appears, `claude_code_rc_spawn_mode: same-dir` is the fallback. |
| The branch lands as `worktree-<name>` under `.claude/worktrees/` | Determines whether `prune_worktrees.py` collects them or they accumulate. |
| The host does not fight terminal sessions for Remote Control | Every terminal session on the host also claims Remote Control via `remoteControlAtStartup`. |
| A phone-spawned session gets real auto mode | Claude Code has a distinct headless classifier path. If auto degrades without a TTY, `claude_code_rc_permission_mode: default` is the fallback — answering prompts remotely is what Remote Control is for. |

## Capacity bounds sessions, not memory

`claude_code_rc_capacity` bounds session *count*. The memory belongs to what a session spawns, and
nothing about a session's size follows from there being ten of them. A cgroup holding a few live
sessions can run to hundreds of processes and exhaust the system's swap. The cgroup then stalls in
reclaim and remote control is unreachable while the unit reads `active (running)`.

`MemoryHigh` does not prevent that, because it throttles rather than caps: the cgroup keeps
allocating into swap past the threshold. A throttled cgroup's anon pages go to swap, so an
unbounded `memory.swap.max` leaves the throttle with no ceiling. `claude_code_rc_memory_swap_max`
is the ceiling.

**Why the swap cap is not 0.** With no swap outlet nothing reclaims the cgroup's anon pages and,
with `MemoryMax` deliberately infinity, the terminal state becomes the global OOM killer choosing a
victim by badness anywhere on the box. The cap converts theft of the whole box's swap into a bounded
share plus harder reclaim throttling; it does not make the cgroup safe.

## The login-session scope

A session started with `claude agents` from an interactive shell lands in
`user.slice/user-<uid>.slice/session-<n>.scope`. That scope reads none of the unit's
environment, `MemoryHigh=` or `MemorySwapMax=` lines and, depending on how the shell was
reached, may not load `~/.claude/settings.json` either. The uid in the scope path is the uid
`claude_code_login_uids` lists, and `claude_code_login_uid` defaults to 1000.

The two artifacts that close it differ in timing. The slice drop-in applies live to an
already-running session on `daemon-reload`, while the `environment.d` file only takes effect at the
*next* login.

## Sharing a variable is not sharing a cap

Rendering `claude_code_rc_memory_high` at two render sites reads as one 8G bound and is two.
`claude-rc.service` and `user-<uid>.slice` are cgroup siblings under *different* parents
(`system.slice` and `user.slice`), so neither sees the other's usage and the fleet's real throttle
point is the sum. Per-plane swap caps add the same way, so a per-plane 2G is a 4G fleet ceiling.

## Why the fleet bound lives on `user.slice`

**`user-<uid>.slice` cannot be reparented, and that is what picked the shape.** A slice's parent is
its *name*: systemd.slice(5) says "The name of the slice encodes the location in the tree," and for
a slice unit `Slice=` accepts "the only accepted value ... the parent slice." `systemd-logind`
creates the slice under that name, so nothing in the role can move it. The unit the role *does*
control is the one that moves instead.

**Nesting the `claude rc` unit one level deeper — inside `user-<uid>.slice` — would also share a
parent, and is unsafe here.** That slice is `StopWhenUnneeded=yes`
(`/usr/lib/systemd/system/user-.slice.d/10-defaults.conf`) and this host has `Linger=no`
(`loginctl show-user ubuntu`), so `logind` stops it at the last logout — and a unit with `Slice=`
gains an implicit `Requires=` on its slice (systemd.resource-control(5)), so that host would be
stopped with it, and `Restart=always` does not bring back a dependency-stopped unit. `user.slice`
measures `StopWhenUnneeded=no`. The role also enables linger on every session host, so
`user-1000.slice` does not stop at logout; the placement stands on the first reason alone.

**A system service in a user-tree slice is systemd's own pattern**, not a workaround: `user@.service`
and `user-runtime-dir@.service` both ship `Slice=user-%i.slice`, and systemd.special(7) describes
`user.slice` as holding "all user processes and services started on behalf of the user," which is
what this unit is: a per-user install that runs as `claude_code_user`, with that user's `HOME` and
OAuth token.

**`memory.high` counts page cache**, so the fleet touches this bound during an ordinary multi-agent
pytest fan-out and reclaims cache. The anon budget is what the number is derived from, not what it
throttles on, and the derivation itself lives in `defaults/main.yml` beside
`claude_code_fleet_memory_high`: 28.2 GiB of RAM, a 10 GiB budget for the homelab plane's anon,
`SUnreclaim` and a page-cache floor, against a measured 24h fleet peak of 11.2 GiB. Re-derive it
there, not from this page.

## Verifying a caps deploy from `cgroupfs`

The parent drop-in applies live on `daemon-reload`, so it lands on already-running login sessions.
The `Slice=` line does not migrate a running unit — it takes effect at the next start, so the deploy
that changes it restarts the `claude rc` host and drops the sessions it spawned. Verify from
`cgroupfs` rather than from `systemctl show`, which reports the configured slice before the restart
has moved the processes:

```bash
systemctl show -p Slice -p MemoryHigh -p MemorySwapMax claude-rc.service user.slice user-1000.slice
cat /sys/fs/cgroup/user.slice/memory.high /sys/fs/cgroup/user.slice/memory.swap.max
ls -d /sys/fs/cgroup/user.slice/claude-rc.service
```

## The webhook stays out of the unit body

systemd serves unit content over the system bus. `systemctl show claude-rc-alert -p ExecStart`
prints an interpolated Discord webhook to any local user with no sudo, while `cat` on the file is
`Permission denied`, so a `0600` mode protects nothing. The alert unit therefore reads the webhook
through an `EnvironmentFile=`. `ansible/roles/setup/gitops_deploy/tests/test_systemd_unit_secrets.py`
walks every `*.service.j2` in the repo, so a new unit cannot take the unsafe shape.

## Each alert `curl` retries

Each alert `curl` carries `--retry 5 --retry-delay 10 --retry-all-errors`, because a unit that
sends its page once loses the page to a transient network fault. Plain `--retry` is not enough:
with curl 8.5 it retries a failed lookup but not a refused connection, and a fault at the moment of
a failure can produce either. systemd labels any exit 6 `NOTCONFIGURED`, which hides a resolve
failure. The cost is that a dead webhook (HTTP 404) retries for about a minute before the unit
fails, and a oneshot unit has no start timeout (`TimeoutStartUSec=infinity`), so the retries
cannot be cut short. `test_alert_units_retry_a_failed_delivery` in the same test file holds every
`*-alert.service.j2` to the flags.

## What sees an expired login (#4618)

Measured on daniel-box on 2026-10-12 against Claude Code 2.1.296. The login can be dead when the
host starts, or it can die while the host is registered. The crash alert covers the first case,
and nothing covers the second.

**A login that is dead at start pages through `OnFailure=`.** With no credentials, `claude rc`
exits 1 at once. This is the output with an empty `HOME` and `env -i`:

```text
Error: You must be logged in to use Remote Control.

Remote Control is only available with claude.ai subscriptions. Run `claude auth login` to sign in with your claude.ai account.
exit=1
```

`Restart=always` starts the host again ten seconds later. systemd 255 records each exit as a
failure, so `OnFailure=` starts `claude-rc-alert.service` on every exit and `Restart=always` does
not hide it. The journal shows both halves. From 2026-10-05 21:54 to 2026-10-08 21:54 UTC,
`claude-rc.service` exited with status 1 between 7,698 and 8,429 times a day, and
`claude-rc-alert.service` ran between 1,540 and 1,686 times a day. That loop came from the
network outage the alert template records (#3902), not from a login, and the same outage lost its
pages.

With the network up, that loop would have paged Discord every ten seconds. The alert therefore
pages once and then skips for `claude_code_rc_alert_quiet_minutes` (360) after each delivered
page (#4676). A loop that lasts days pages four times a day. A second, separate crash inside the
window is not paged, and the page text says so. `RestartMode=direct` with a start limit on the
host was rejected: it pages once by leaving the host `failed`, and the weekly `try-restart` never
starts a failed unit, so the host would have stayed down after the #3902 outage cleared. A token that is present but refused takes the same path: in the 2.1.296 bundle a 401 at
registration throws `BridgeFatalError`. That path was read from the bundle, not observed.

**A login that dies while the host is registered pages nothing.** The bridge polls for work with
an environment secret, not with the OAuth token. The host uses the OAuth token only to register,
and again to renew that secret before it expires. Phone sessions fail at once, because each one
calls the API with the OAuth token, but the host keeps polling. At the renewal, the host writes
"Could not renew the Remote Control credential because your claude.ai login is no longer valid"
only when it runs with `--verbose`, and keeps working until the secret expires. Only then does it
re-register, take the 401 and start the loop above. The secret's lifetime is unmeasured. The
weekly `try-restart` caps the wait at a week, because a restart registers again.

Four candidate signals were ruled out for the second case:

- `~/.claude/daemon.log` is the background supervisor's log, and the systemd-hosted host does not
  write it. The agent's copy was last written on 2026-10-10 18:07, before the running host started
  on 2026-10-11.
- `~/.claude/daemon-auth-status.json` is sticky. The supervisor writes `auth_required` when a
  refresh fails and never rewrites the file afterwards. The operator's copy still read
  `auth_required` from 2026-09-18 05:32, although the same `daemon.log` records "token found via
  keychain re-check" at 08:58 that day. The agent has no such file.
- `claude auth status` reports `loggedIn: true` when a token is present. It does not ask the
  server whether the token works.
- `claudeAiOauth.expiresAt` in `.credentials.json` lapses on a healthy idle host, because nothing
  refreshes the access token between renewals of the environment secret. A check on it would page
  every quiet night.

Three changes would close the gap, and each one costs something the operator has to accept:

- `--verbose` on the unit would put the renewal failure in the journal for a check to match. It
  detects only at the renewal, whose interval is unmeasured, and the volume of the other verbose
  lines is unknown. Measuring either needs a host restart, which drops live phone sessions.
- A daily `try-restart` instead of a weekly one would make a dead login loop within a day, and
  `OnFailure=` would page it. It also drops live phone sessions every day.
- An authenticated probe as the agent user would detect a dead login within one run. Every form
  found spends inference (`claude -p`) or reads `.credentials.json` outside Claude Code's own
  locking.

## Why the weekly restart uses `try-restart`, not `RuntimeMaxSec=`

`claude-rc-restart.timer` exists to pick up the binary Claude Code updates in the background,
which a long-lived process does not reread on its own. `RuntimeMaxSec=` looked like the natural
mechanism for that: restart the unit automatically once it has run long enough. systemd records a
`RuntimeMaxSec=` expiry as a unit failure, though, so that mechanism would page the crash alert
every week for a planned restart. `try-restart` avoids that failure, and it avoids the other risk
a plain `restart` carries: starting a host that `claude_code_rc_enabled` deliberately keeps
stopped.

## The background-shell pressure reaper

Claude Code registers `process.on("memoryPressure", ...)` and kills every running backgrounded Bash
task with the reason `memory_pressure`, surfacing as "stopped because the system is running low on
memory." The handler reads no threshold — it is a pass-through on Node's event, and Node derives that
event from the **cgroup**, so `free -m` on the host measures the wrong scope and reads clean. The
kill arrives as a task notification rather than an error in the log file, so a
`land.sh --arm-merge` reaped between the arm and the CI wait leaves the PR merged and undeployed
with the session reading clean. `claude_code_rc_disable_bg_shell_pressure_reap` turns the reaper
off for that reason.
