# claude-rc caps — the derivations behind the memory bounds

The role doc `hosts/components/claude-code/CLAUDE.md` keeps the rules. This page keeps the
live phone check, the arguments that settled which slice carries the fleet bound, and the
reasoning behind a few unit-shape rules.

## The phone check no test can run

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
| The branch lands as `worktree-<name>` under `.claude/worktrees/` | Determines whether the worktree pruner collects them or they accumulate. |
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
pytest fan-out and reclaims cache. The derivation lives in `defaults/main.yml` beside
`claude_code_fleet_memory_high`, and daniel-server's lives in its host_vars. Re-derive it there,
not from this page.

### The fleet cap and the 8 GiB `MemAvailable` floor

Neither cap holds the operator's 8 GiB `MemAvailable` floor when the fleet's memory is anon.
Both hosts fell below it once:

- daniel-box reached 2.7 GiB on 2026-11-18 with the fleet at 17.3 GiB.
- daniel-server reached 1.4 GiB on 2026-11-14 with the fleet at 23.1 GiB.

[#9199](https://github.com/DanielH2018/server/issues/9199) re-derived both caps from a
measurement per plane.

- **`MemAvailable` is close to `MemTotal` minus each plane's anon and kernel memory.**
  The planes are `user.slice` (the fleet), `kubepods.slice` and `system.slice`. On
  2026-11-22 that sum came within 0.6 GiB of `MemTotal - MemAvailable` on both hosts.
  Page cache counts as available.
- **`memory.high` bounds anon, file and kernel together.** So the worst-case floor is
  `MemTotal - homelab (anon + kernel) - cap`. The old derivation added the fleet's whole
  `memory.current`, page cache included, as if it were already unavailable. That error
  hid a negative worst case.
- **The worst-case floor is below zero on both hosts.** daniel-box gives
  27.7 - 12.6 - 20 = -4.9 GiB, and daniel-server gives 30.5 - 8.3 - 24 = -1.8 GiB. The
  homelab terms use the 14-day peak of `kubepods.slice`'s anon.
- **A cap that holds 8 GiB of all-anon fleet memory is about 7 GiB on daniel-box and 14 GiB
  on daniel-server.** Those caps are far below what an ordinary busy day needs. On
  2026-11-20, six coordinator projects ran. daniel-box's fleet peaked at 15.9 GiB and
  `MemAvailable` stayed at or above 9.8 GiB. daniel-server's fleet peaked at 11.7 GiB and
  `MemAvailable` stayed at or above 20.7 GiB. The floor holds only while much of the
  fleet's memory is page cache.
- **The swap in the daniel-box episode was not the pods'.** Host swap reached 7.6 GiB. The
  fleet held its 2 GiB, and cAdvisor reports at most 2 MiB of swap in `kubepods.slice`.
  The other 5.6 GiB was mostly in `system.slice`. MemAvailable bottomed out between 20:00
  and 22:00 UTC. systemd's stop records in daniel-box's journal give three units' swap
  peaks in that window: `homelab-docs-refresh.service` 2.8 GiB, `k3s.service` 1.2 GiB and
  `homelab-aide-check.service` 0.76 GiB. Together they account for about 4.8 GiB.

`claude-cgroup-metrics.sh` writes `claude_host_plane_memory_bytes{plane, kind}` for each
plane, with `kind` one of `anon`, `file`, `kernel` and `swap`. cAdvisor reports
`kubepods.slice` but never `system.slice`, where k3s runs, so this family is the only source
for the `system` plane. It is a separate family from `claude_cgroup_*` because the
`ClaudeCgroup*` rules select that prefix with no `cgroup` filter, and a homelab plane in
it would page as the fleet.

### Protecting the homelab plane with `MemoryLow`

The operator ruled on 2026-11-22
([#9890](https://github.com/DanielH2018/server/issues/9890)) to keep both fleet caps. The
homelab plane is protected directly instead. The k3s-server component ships a `MemoryLow=`
drop-in for `kubepods.slice`, `system.slice` and the k3s unit (`k3s.service` on daniel-box,
`k3s-agent.service` on daniel-server). k3s-agent ships the same files.

The kernel reclaims a cgroup below its `memory.low` only after it has reclaimed every
unprotected cgroup. `user.slice` sets no `memory.low`, so under pressure the fleet's pages go
first. Each value is the plane's 14-day peak of anon plus kernel memory, rounded up to
0.5 GiB. A larger value would also protect page cache that the fleet needs more.

| Cgroup | daniel-box | daniel-server | Measured peak (box / server) |
|---|---|---|---|
| `kubepods.slice` | 10G | 8G | cAdvisor `rss` 9.2 + kernel 0.8 / 7.3 + 0.4 GiB |
| the k3s unit | 2560M | 1G | cAdvisor `rss` 2.2 / 0.76 GiB |
| `system.slice` | 3G | 1536M | the k3s value + the other units' steady 0.5 / 0.25 GiB |

`k3s_kubepods_memory_low` in `hosts/components/k3s-server/defaults/main.yml` holds
daniel-box's derivation. daniel-server's values are in its host_vars. The `system` term had
less than a day of `claude_host_plane_memory_bytes` behind it on 2026-11-22. The 14-day
re-read of the issue's Verify-by query is the check on it.

- **The k3s unit has its own claim.** cgroup2 is mounted with `memory_recursiveprot` on both
  hosts. Under that option a parent's protection is shared among the children that claim
  none, in proportion to their usage. Without its own claim, k3s would share `system.slice`'s
  3G with the docs-refresh run's pytest, which peaked at 8.5 GiB on 2026-11-22. A child's
  effective protection never exceeds its parent's, so `system.slice` stays at or above the
  k3s value.
- **kubelet resets `kubepods.slice` at each start.** kubelet owns `kubepods.slice` as a
  transient unit and runs set-property with `MemoryLow=0` and `MemoryMin=0`. systemd writes
  those to `/run/systemd/transient/kubepods.slice.d/50-*.conf` and applies them at once.
  The homelab drop-in is `60-homelab-memory-low.conf`, which sorts later, so it wins at
  every `daemon-reload`. `homelab-kubepods-memory-low.path` watches kubelet's
  `50-MemoryLow.conf` and runs that reload whenever kubelet rewrites it. The reload is
  deliberately not set-property, which would rewrite the watched file and trigger itself.
- **No `MemoryMin=`.** A hard floor on the homelab plane turns fleet pressure that reclaim
  cannot absorb into a global OOM.

`MemoryLow` does not guarantee the 8 GiB floor:

- It decides which cgroup gives up pages first. It creates no `MemAvailable`. The fleet's
  anon can leave RAM only through its 2G swap share, so all-anon fleet memory past
  `MemTotal - homelab - 8 GiB` still breaks the floor.
- It does not steer the OOM killer. kubelet gives a burstable pod an `oom_score_adj` of
  2-999 and a best-effort pod 1000. `claude_code_fleet_oom_score_adjust` steers it instead,
  as *The OOM killer's victim* below says.

So no `MemoryLow` value meets the ruling's condition that the protected slices keep the
floor with the fleet at its cap. With all-anon fleet memory at the cap, daniel-box gives
27.7 - 13 - 20 = -5.3 GiB and daniel-server gives 30.5 - 9.5 - 24 = -3 GiB, and a larger
protection only widens the gap. The floor stays a typical-day floor until the operator
re-rules on it ([#9993](https://github.com/DanielH2018/server/issues/9993)).

The operator ruled on 2026-11-22 (option 1 of #9993) that the floor is a typical-day floor,
with both fleet caps unchanged and no systemd-oomd kill. The `HostMemoryAvailableLow` alert is
its check: it warns when `node_memory_MemAvailable_bytes` stays under 8 GiB for 10 minutes on
daniel-box or daniel-server. If it fires during an episode in which a pod was evicted or
OOM-killed, the next step is systemd-oomd on `user.slice`
([#10000](https://github.com/DanielH2018/server/issues/10000)).

To verify a deploy, read the configured value and the kernel's value. Read
`kubepods.slice` again after the next k3s start, which is when kubelet resets it:

```bash
systemctl is-active homelab-kubepods-memory-low.path
systemctl show -p MemoryLow -p DropInPaths kubepods.slice system.slice
systemctl show -p MemoryLow k3s.service   # k3s-agent.service on daniel-server
cat /sys/fs/cgroup/kubepods.slice/memory.low /sys/fs/cgroup/system.slice/memory.low
```

### The OOM killer's victim

Every fleet process runs at `oom_score_adj` 1000, the kernel's maximum, so a global OOM picks
a fleet process before a pod. `claude_code_fleet_oom_score_adjust` in the component's
`defaults/main.yml` holds the value. `OOMScoreAdjust=` is an exec option that a slice drop-in
cannot carry, so the value reaches the fleet by four paths:

- `homelab-claude-rc.service` and `homelab-claude-bg-resume.service` set `OOMScoreAdjust=`, and
  the sessions they start inherit it.
- A drop-in on `user@<uid>.service` sets the user manager's value for the agent's uid 900 and
  the operator's uid 1000. Its units, among them every fan-out batch `systemd-run --user`
  starts, inherit it. uid 1000 gets it on daniel-box too: its login caps leave
  `user-1000.slice`, but fan-out and background jobs still run there as uid 1000.
- The agent's login profile writes the value to `/proc/self/oom_score_adj`, because an ssh
  login inherits sshd's 0. Raising the value needs no privilege.
- The component's `lines:` block in `/etc/zsh/zshenv` writes the same value for every zsh the
  operator starts. The operator is the only zsh user on either host, and the operator's own
  profile belongs to the dotfiles repo. zsh reads `zshenv` for a command passed to
  `ssh host cmd` too. The desktop app starts its remote server that way. Only a login shell
  reads `zprofile`, so a block there left every desktop session at sshd's 0. The component
  declares a comment-only block in `zprofile`, because the lines arm never removes the block
  the hosts already hold.

The bias is small. The kernel's badness score adds `adj * totalpages / 1000` to a process's
RSS, so 1000 against a burstable pod's 999 is worth about 30 MiB on these hosts. A pod
process with a far larger RSS than any fleet process can still be the victim.

Each value takes effect at the next start of what carries it. Reconcile restarts neither unit
nor any user manager, so read the live value after the next reboot. To check, run
`systemctl show -p MainPID homelab-claude-rc.service user@900.service`, then read
`/proc/<pid>/oom_score_adj` for each process ID it prints. A desktop session takes the value
when the app's remote server next starts.

### What the caps did not cover on 2026-11-25

k3s on daniel-box exited at 15:08:10, 15:21:13 and 15:24:35 UTC
([#10119](https://github.com/DanielH2018/server/issues/10119)). Each exit followed a lost
leader lease. The process that exited at 15:21:13 logged `leaderelection lost/stopped` at
15:17:29 and nothing after it, so it took nearly four minutes to stop. CPU was not the
shortage: CPU pressure stayed at or below 0.36, and the weights in *The fleet's CPU weight*
were live (`user.slice` 20, `system.slice` and k3s 1000). Memory was the shortage. The figures
come from Prometheus, and the pressure and fault rows are two-minute rates:

| | 15:00 | 15:06 | 15:14 | 15:20 |
|---|---|---|---|---|
| Fleet anon (GiB) | 5.3 | 11.2 | 13.3 | 14.6 |
| Fleet `memory.current` (GiB), against its 20 GiB `MemoryHigh` | 9.8 | 15.4 | 17.5 | 17.8 |
| Fleet swap (GiB), against its 2 GiB cap | 1.95 | 2.00 | 2.00 | 2.00 |
| `system.slice` anon (GiB) | 1.9 | 1.0 | 0.06 | 0.09 |
| `kubepods.slice` anon (GiB) | 6.6 | 6.6 | 6.2 | 5.9 |
| Host `MemAvailable` (GiB) | 10.8 | 2.9 | 1.9 | 1.9 |
| Memory pressure, full | 0.00 | 0.17 | 0.90 | 0.79 |
| Major faults per second | 1 | 51,600 | 35,000 | 32,500 |

- **The fleet's `MemoryHigh` never acted.** The fleet's `memory.events` `high` counter did not
  move. The shortage came below the cap.
- **k3s was the plane that gave up its anon.** Its working set fell from 2.2 GiB at 15:00 to
  0.27 GiB at 15:06, and its swap rose from 0.4 to 1.9 GiB, under a `MemoryLow` of 2.5 GiB.
  The fleet's swap was at its cap, so the fleet's anon could not leave RAM. kubelet gives a pod
  no swap, so a pod's anon could not either. `system.slice` has no swap limit, which left it
  as the only slice with anon the kernel could move. `MemoryLow` is best effort and did not
  stop that.
- **k3s's lease requests timed out while it faulted its pages back in.** At the 15:24 exit,
  which came after the tests had ended, the host still took 11,700 major faults a second.
- **The OOM killer ran four times, between 15:09:42 and 15:20:54.** It killed Sonarr, a
  Longhorn instance-manager, a `next-server` and one 58 MB `claude` process. Two fleet paths
  ran below every burstable pod. The desktop sessions ran at 0 because `zprofile` held the
  block. uid 1000's user manager ran at 100 because it started before its drop-in shipped.
- **`HostMemoryAvailableLow` fired from 15:13 to 15:21, and a pod was OOM-killed inside it.**
  That is the condition the ruling above set for systemd-oomd on `user.slice`
  ([#10000](https://github.com/DanielH2018/server/issues/10000)).
- **The worker cap does not reach these sessions.** `PYTEST_XDIST_AUTO_NUM_WORKERS` is unset in
  an `ssh host cmd` shell, and `pytest -n 8` sets its own count anyway.

The OOM bias alone cannot keep k3s running. The first exit came 92 seconds before the first OOM
kill. The operator decides what keeps k3s in memory
([#10126](https://github.com/DanielH2018/server/issues/10126)). A lower fleet cap, `MemoryMin` and
systemd-oomd each contradict a ruling above. `MemorySwapMax=0` on k3s has none, but it takes k3s's
last outlet for reclaim, which is the trade the *No `MemoryMin`* bullet rejects.

## The fleet's CPU weight

The same drop-in sets `CPUWeight=` from `claude_code_fleet_cpu_weight`. At the root cgroup,
`user.slice` competes with `system.slice`, where k3s runs, and `kubepods.slice`, which kubelet
weights at 625 on daniel-box's 16 cores.
On 2026-11-18 a dozen sessions' pytest runs saturated daniel-box's 16 cores. etcd applies then
took 150-480 ms, and kube-controller-manager lost its leader lease three times in five minutes,
each time exiting k3s ([#9060](https://github.com/DanielH2018/server/issues/9060)).

- **A drop-in on `user-1000.slice` would miss part of the fleet.** `claude-rc.service` and
  `claude-bg-resume.service` sit directly under `user.slice`, beside the login slices.
- **A weight on `k3s.service` alone ranks k3s only inside `system.slice`.** The k3s components
  set one too (`k3s_cpu_weight`), because the docs-refresh run's pytest and MkDocs are
  `system.slice` siblings of k3s.
- **A weight on `k3s.service` does not rank `system.slice` against the pods.** At systemd's
  default of 100, busy pods outweighed the slice that holds etcd and the controllers about six
  to one ([#9075](https://github.com/DanielH2018/server/issues/9075)). Both k3s components
  therefore set `system.slice`'s own weight, `k3s_system_slice_cpu_weight`, above kubelet's
  `kubepods.slice` weight on either node.
- **A weight divides CPU only under contention.** An idle box still gives the fleet every core,
  which is why neither drop-in sets `CPUQuota=`.

To verify a weight deploy, read the kernel's value, not only the unit's:

```bash
systemctl show -p CPUWeight user.slice system.slice k3s.service
cat /sys/fs/cgroup/user.slice/cpu.weight /sys/fs/cgroup/system.slice/cpu.weight \
    /sys/fs/cgroup/system.slice/k3s.service/cpu.weight
```

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
through an `EnvironmentFile=`.

## Each alert `curl` retries

Each alert `curl` carries `--retry 5 --retry-delay 10 --retry-all-errors`, because a unit that
sends its page once loses the page to a transient network fault. Plain `--retry` is not enough:
with curl 8.5 it retries a failed lookup but not a refused connection, and a fault at the moment of
a failure can produce either. systemd labels any exit 6 `NOTCONFIGURED`, which hides a resolve
failure. The cost is that a dead webhook (HTTP 404) retries for about a minute before the unit
fails, and a oneshot unit has no start timeout (`TimeoutStartUSec=infinity`), so the retries
cannot be cut short.

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
