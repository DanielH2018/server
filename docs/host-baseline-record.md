# Host baseline record

The traps, derivations and measured incidents behind the `initial_setup` role — the host
baseline every machine in the homelab runs. `ansible/roles/setup/initial_setup/CLAUDE.md`
carries the rules and the tag inventory; this page is the working-out, read when you edit the
block it covers.

`tasks/` is the role's own inventory: one file per block tag, named for its subject. Read the
task file for what a block does. What follows is what a task file does not say on its own.

## What each block tag covers

The role's `CLAUDE.md` lists the tag names; these are the ones whose scope is not obvious from
the name:

- `pi-swap` — the Pi swapfile plus its watchdog-stop preamble.
- `apt-upgrade` — the Docker-engine hold, then the full dist-upgrade. The `docker_install`
  role's `CLAUDE.md` has why the hold sits here.
- `tooling` — uv and the CLI tools; `packages` is the apt baseline.
- `crons` — every cron and kuma-check timer in `crons.yml` and `accounting.yml`. The
  rkhunter and AIDE crons answer to `rkhunter` and `aide` instead. Each `crons` task also
  answers to a subject tag, so `--tags crons` is never the only way to reach it:
  `secret-rotation` (the audit, the weekly rotation and their stamp), `weekly-restart`, `prune`, `ansible-log`,
  `worktree-sweep`, `cert-expiry` (the retired TLS watch's removal), `infra-map`, `firmware`,
  `setup_drift`, `loki_route_witness`, `docs`, `evals` and `apt-hygiene` (autoremove and
  dpkg-purge). The Healthchecks ping key answers to both `prune` and `weekly-restart`, because
  both crons source it. `ansible/tests/setup/test_initial_setup_tasks_have_a_subject_tag.py`
  refuses a task whose only tag is `crons`.
- `tuning` — the server's CPU governor and swappiness.
- `debloat` — the server's LXD-snap removal and the networkd-dispatcher mask on both hosts.
- `firewall` — UFW. `kernel-modules` is the blacklist plus WireGuard.
- `igpu` — i915 GuC for QuickSync, on `has_igpu` hosts only.
- `accounting` — sysstat and acct.

## Tooling, pins and the root HOME trap

- **Every `tooling` and `git-hooks` task is gated on `dev_tooling_hosts`** (daniel-box and
  daniel-server). daniel-pi holds a checkout but nobody commits from it, and the tag would put
  uv and a pinned Python on a 512 MB Zero 2 W (#1726). Membership is enforced by
  `ansible/tests/setup/test_dev_tooling_hosts.py`.
- **Every tool this role installs is pinned, and a bump needs its digest regenerated** (#2148).
  uv's version, installer URL and sha256 sit together at
  `initial_setup_uv_version` in the role's `defaults/main.yml`, so
  `scripts/validate/asset_pins.py` can render the URL and produce the hash an unattended
  renovate_agent run has no `curl` to produce — `--only initial_setup_uv_installer` (#2301).
  `ansible-core`, `ansible-lint`, `prek` and Vale each equal a twin in `pyproject.toml`,
  `prek.toml` or CI (`ansible/tests/repo/test_host_tool_pins_match_their_twins.py`,
  `ansible/tests/repo/test_vale_matches_the_ci_pin.py`), and every install is version-gated
  rather than `creates:`-gated, so a bump replaces the tool.
- **This play becomes root, so `~` is `/root` — SSH paths are absolute only** (#2413, ratcheted
  by `ansible/tests/setup/test_ssh_dir_paths_are_absolute.py`). The home-dir resolver task
  exists for the same reason, and its `register:` is why it carries every consumer's tag —
  `[tooling, git-hooks]`, the shape the role doc's fact-dependency rule names. That rule is a
  check rather than prose since the GitOps deployer began deriving and applying narrow setup
  tags (#3120). ENFORCED:
  `ansible/tests/setup/test_register_producers_carry_consumer_tags.py::test_every_register_producer_carries_its_consumers_tags`,
  over every setup role rather than this one.
- **`become` vs HOME:** the `Resolve the deploy user's home directory` task exists because
  `ansible_facts.env.HOME` is root's under the play's `become: true`, while uv and the per-user
  tooling must install for the unprivileged deploy user. Keep a new per-user task on the
  resolver rather than on `env.HOME`.

## SSH, UFW and sudo

- **The `Match sys_user` block sets `AllowTcpForwarding all`, not `local`** (#397), plus
  `AllowStreamLocalForwarding remote` and `StreamLocalBindUnlink yes`, for the clipboard
  bridge's reverse unix-socket forward: sshd builds the channel layer from
  `AllowTcpForwarding` alone, so `local` refuses every remote forward before
  `AllowStreamLocalForwarding` is read. The comment above the block in `tasks/access.yml`
  carries the derivation and the log line that tells the two refusals apart.
- **The UFW LAN allow must sort above the SSH limit rule**, which is why its task declares
  `insert: 0` / `insert_relative_to: first-ipv4`: UFW takes the first matching rule, so an
  allow appended below the limiter is inert (#508).
  `ansible/tests/setup/test_ssh_rate_limit_lan_exempt.py` pins the exemption and its ordering.
  The exemption exists because `limit` REJECTs a source at 6 connections in 30 seconds on a
  rolling window, counting SUCCESSFUL ones, so retries sustain the block — parallel Claude
  sessions over ssh crossed it twice. fail2ban's sshd jail is what answers a credential attack.
- **UFW owns a SECOND sysctl file**, `/etc/ufw/sysctl.conf` (`IPT_SYSCTL` in
  `/etc/default/ufw`), re-applied on every `ufw enable` or `reload`. Ubuntu ships it with
  `log_martians=0`, so before #2977 this role's loop wrote 1 and `Enable UFW firewall` wrote 0
  back twenty tasks later.
- **The credential cache is what stops every `!` command re-prompting.**
  `/etc/sudoers.d/10-timestamp` sets `timestamp_type=global` with a 60-minute timeout, so one
  authentication covers every pane and every tty-less shell. It is written with
  `validate: visudo -cf %s`, because a malformed drop-in locks the escalation path out, and
  that path is the only write path to the cluster.

## Unattended upgrades: two traps

Use `Origins-Pattern`, not `Allowed-Origins` — the legacy form is rewritten to `o=X,a=Y` and
matches only a repo publishing a `Suite:` field, which the gh repo does not
(`ansible/tests/setup/test_unattended_origins_pattern.py`). And use the `::` append syntax: a
`{ ... }` block reads as a replacement and drops the `-security` and ESM pockets. Verify both
`apt-config dump` keys — `Allowed-Origins` must still list `-security`, `Origins-Pattern` the
extras from `unattended_upgrades_origins_patterns`, which is `[]` for security-only.

## Integrity scans, Postfix and the Pi's swap file

- **AIDE and rkhunter are scheduled against each other, and the package's own timer is masked.**
  `dailyaidecheck.timer` duplicated the weekly cron nightly with broken mail alerting, at about
  1h20m of CPU per night on the Pi. Both weekly scans run `nice -n19 ionice -c3`, staggered to
  AIDE Monday 03:00 and rkhunter Wednesday 02:00, because they used to overlap at full priority
  for over an hour each on the Pi. The AIDE database init is the slow part of a first run on any
  host — about 8 minutes, with progress monitoring, so expect a long pause rather than a hang.
- **Postfix binds `inet_interfaces = loopback-only`** and needs `notify: Restart Postfix`: a
  reload does not rebind the socket, so the `0.0.0.0:25` listener would survive it.
- **The Pi's swap file is bring-up, not tuning.** It is gated on `has_low_memory_board` and the
  hardware watchdog is stopped around it, because heavy apt on a 512 MB Zero 2 W OOMs without
  disk swap. Pi-only packages gate on `has_raspi_kernel` instead — the board and its memory are
  two separate facts.
- Two templates carry host-specific config: `templates/98_aide_local.conf.j2`, the AIDE
  exclusions for the Docker homelab, and `templates/fail2ban_homelab.conf.j2`, the fail2ban
  jail. Pi-only tasks are individually `when:`-guarded rather than block-scoped, so a server run
  simply skips them.

## Setup-plane drift reader (`setup_drift` tag)

`setup-drift-check.sh` answers two questions daily on the hosts named in
`setup_drift_check_hosts` (`group_vars/all.yml`): is the `copy:`-deployed code here identical to
its repo source, and has any `template:`-rendered setup script's SOURCE changed since this host
rendered it?

It runs from `kuma-check-setup-drift.timer` since 2026-09-19, and so does the daily
secret-rotation audit (`kuma-check-secret-rotation-audit.timer`, on the `has_gitops` host
only): both import the `common` role's `kuma_check_timer.yml`, exit 1 after a down push, and rerun every 30 min
under `Restart=on-failure` until they exit 0. For the audit that includes the exec'd
`secret_rotation.py audit --push`, which returns 1 when it pushed `down`. Each import's
`kuma_check_state` follows the host gate, so a host dropped from the list, or one without
`has_gitops`, loses the timer and the cron it replaced.

**Why it exists separately from `manifest-prune-check.sh`.** That check answers the same two
questions plus an orphaned-cluster-object arm, but it is installed by
`roles/setup/k3s/tasks/health-crons.yml`, imported only from that role's `main.yml`, which
`k3s-bringup.yml` asserts onto `k3s_server_hosts`. So it exists on daniel-box and nowhere else,
while daniel-server renders the entire UPS shutdown chain. Confirmed live 2026-08-29:
daniel-server had no `/var/lib/homelab/setup-render-manifest.d` at all, and its
`/etc/nut/upsmon.conf` was dated Aug 17 against a template changed on 2026-08-28 (review M-10).

- **The two arms are not reimplemented** — both readers source `files/setup-drift-lib.sh`,
  copied to `/usr/local/lib` like `kuma-push-lib.sh`. Two copies of a drift check are free to
  drift from each other, which is the fault a drift check reports.
- **It does NOT carry the orphan arm.** That needs `/etc/rancher/k3s/manifests` and the control
  plane's staged set; an agent node has neither, and an arm that structurally cannot fire reads
  as coverage.
- **A third arm reports the checkout's age**, because the render arm compares a render against
  the tree on THIS host and a host without `gitops_deploy` does not refresh that tree —
  daniel-server was measured 39 commits behind origin on 2026-08-17. A stale checkout makes the
  stamp and the template agree, so the arm would read green exactly when the host is furthest
  behind. Past `setup_drift_tree_max_days` (14) the age is its own DOWN; an unreadable checkout
  is a DOWN too, never a pass.
- Pushes the "Setup Plane Drift (agent hosts)" Kuma tile. Its token is in
  `CROSS_HOST_PUSH_TOKENS`: the cron is on daniel-server and the tile deploys from daniel-box,
  so no single `rotate --deploy` can move both halves.

### Verify a cron arm as the user cron runs it

The age arm reads the tree with `git -c safe.directory="$REPO_DIR"`, and the exception is
load-bearing. The cron runs as root against a checkout owned by `sys_user`, which is exactly
git's dubious-ownership case, so a bare `git log` exits non-zero and the arm reports "cannot
read the checkout" on every run. It did: the tile was DOWN from the arm's first cron run
(2026-08-30 07:50) until the fix. The 2026-08-29 hand-run that verified the arm ran as `ubuntu`
and passed, which is the whole shape of the miss. `ansible/tests/deploy/test_setup_drift_check.py`
now forces the refusal with `GIT_TEST_ASSUME_DIFFERENT_OWNER=1`, and carries a control asserting
a bare read still fails under it, so the accept half cannot pass because the simulation went
inert. That file EXECUTES the library against a fixture rather than grepping it, and
`ansible/tests/deploy/test_setup_render_manifest.py` holds both consumers to sourcing it.

## Loki read-route witness (`loki_route_witness` tag)

`loki-read-route-health.sh` runs hourly on every host in `loki_route_witness_hosts`
(`group_vars/all.yml` — both prod cluster nodes) and pushes that host's own "Loki Read Route
(<host>)" Kuma tile.

**It is a systemd timer, `kuma-check-loki-read-route.timer`, not a cron, since 2026-09-19.**
The `Schedule the Loki read-route witness` task imports the `common` role's
`kuma_check_timer.yml`, whose service is `Restart=on-failure` with `RestartSec=15min`: the
script exits 1 after it pushes `down`, so a red tile reruns every 15 minutes until the route
answers instead of sitting red until the next hourly slot. The timer is `Persistent=true`, so a
slot missed inside an outage runs at boot; the script's boot-grace arm exits 1 without a push,
and the restart carries the real verdict. `systemctl status kuma-check-loki-read-route` shows
`auto-restart` while it is red, and `journalctl -u kuma-check-loki-read-route` holds the runs.
The import removes the old `Loki read-route witness` cron on every host.

**Why it is a host check and not a monitor-bridge check.** `loki-homelab`'s read route is guarded
by a ClientIP set of node-owned addresses, and its only caller is `probe.py loki-query` or
`loki-labels` running as a host process. A pod prober arrives with a pod IP, so it either fails
for a reason the operator never hits or has to be granted an address no real caller uses.

**Why both nodes.** Which address a host process arrives as depends on where the traefik pod
sits — the same node gives that node's cni0 gateway, another node gives the sender's flannel.1
address. #1693 left the route dead from daniel-server and healthy from daniel-box, so a single
witness would have reported green throughout. Each host pushes its own token, for the reason the
`nut_host` role's secondary watchdog records: two hosts on one token let either host's `up`
satisfy the deadline.

**The verdict is the response body, never the exit code.** `probe.py` builds its curl argv
without `-f`, so a route Traefik refuses returns `404 page not found` with exit **0**.
`files/loki_route_health.py` decides — valid JSON, `status: success`, non-empty `data` — and
the role's `tests/test_loki_route_health.py` holds that 404 as a DOWN case.
`ansible/tests/setup/test_loki_route_witness.py` guards the host list, the per-host tokens, the
push deadline against the timer period, the exit-code contract (a down verdict exits 1), and the
removal arm. Both tokens are in `CROSS_HOST_PUSH_TOKENS`
(`scripts/secrets_mgmt/consumers.py`): the timer is in a role with no deploy tag and the tile
deploys from `k8s/uptime-kuma`, so no single `rotate --deploy` can move both halves.

## The generated-docs refresh cron, in detail

- **Why it weights test modules at all (#2274).** CI's `pytest_shard.py --check-durations` step
  rejects the PR that introduces an unweighted module costing `RUNNER_HEAVY_SECONDS` or more and
  leaves the repair to a human. It says nothing about a lighter one, and an unweighted file is
  packed at the suite median of 0.0s — so without the cron the split is un-skewed rather than
  measured. `--record-missing`, never `--record`: it measures only the files the table lacks,
  and writes the same bytes back when there are none, so an ordinary run leaves no diff.
- **A red suite is not a case degradation saves**: the script runs the suite before its commit
  and exits 1 on a failure, because the prek `pytest` hook that used to cover this fires at
  pre-push (#2827). What degradation saves is the suite being fine and the measurement alone
  failing — the `timeout` firing, or a durations report that parses to nothing. The `timeout` is
  there because this script holds the git-tree lock for its whole life.
- **A generator that writes outside the three staged paths is a defect**: the write is unstaged,
  and an unstaged file in the primary checkout parks every deploy on the box.
- **Its abort valves** are the shared `/var/lock/server-git-tree.lock`, the dirty-tree gate at
  the top (build the site, change no file), the unlanded-branch guard (`publish_pr.py unlanded`,
  read against origin rather than the open-PR list), and the commit-failure stamp under
  `/var/lib/homelab/docs-refresh.d`, which is what lets the next run tell its own leftover dirt
  from a human's.
- **Before adding a fourth staged path**, or a prek hook matching one of the three, check the
  hook cannot fail on generator output — a hook that can wedges this cron on every run, and
  `end-of-file-fixer` and Vale have both already tried.

## The eval sweep, in detail

- **Its mode is binary, with no dial.** Either `anthropic_api_key` exists and the sweep runs
  hermetic, or it is empty and the whole run is a no-op that reports UP with the reason logged
  (`eval-run.sh.j2`'s header). There is no non-hermetic fallback, because a subscription run's
  numbers are noise `evals/trend.py` refuses to trend anyway.
- **The authoritative source is the chezmoi eval engine's own grading**
  (`~/.local/share/chezmoi/evals/run-evals.mjs`) — never a cached or hand-edited report.
- **Its abort valves** are the shared git-tree lock (a dirty tree or another in-flight commit
  means the sweep's result is thrown away rather than recorded against a tree it did not grade)
  and the unlanded-branch guard: an `evals-history/*` branch still on origin skips the run
  rather than stacking a second one, and it reads origin rather than the open-PR list because a
  failed `gh pr create` leaves a branch with no PR.
- **A REGRESSED case still gets committed** — the data is real — but reports the push DOWN with
  the regression named, matching autofix-bridge's "act, but do not launder the result" pattern
  rather than silently dropping the regression or silently blocking the commit.
- **Before widening scope** (grading `run-live.mjs`, changing cadence, adding a case directory
  the loop should skip), read the last few PRs this cron opened (`evals-history/*`,
  squash-merged) for what actually regressed or flaked.

## The journald cap, the rsyslog filter, and what a forensic read loses

The `journald` tag's two files are one change. `50-homelab.conf` caps the journal
(`SystemMaxUse=1G`, `MaxLevelStore=notice`) and sets `MaxLevelSyslog=info`;
`/etc/rsyslog.d/49-homelab-info-filter.conf` then discards info for every facility that reaches
rsyslog *through* journald. Deploying either alone is a defect.

**Why the two levels differ.** journald owns `/dev/log`, so rsyslog only ever sees what journald
forwards, and `Store` and `Syslog` are applied to their sinks independently. `MaxLevelSyslog`
read `notice` from 2026-08-01 to 2026-08-29 and deleted the host's SSH authentication trail:
`Accepted publickey ... SHA256:` and every `pam_unix(...:session)` line is priority info. sshd's
`LogLevel VERBOSE` was correct throughout and irrelevant — an application's log level bounds
what it emits, not what survives. Two things depended on those lines: which key authenticated is
recorded nowhere else, since auditd has the login but never the fingerprint, and the `crowdsec`
node agent tails `auth.log` for the SSH brute-force signal.

**The filter's exemptions are load-bearing.** `auth` and `authpriv` carry the records; `kern`
and `mail` must be exempt because they do *not* arrive via journald — rsyslog reads kernel
messages itself through `imklog`, and postfix writes to its own listen socket — so discarding
their info would remove lines from `kern.log` and `mail.log` that are present today. Measured
cost of the pair: +0.34 MB/day/host, against a +7 MB/day/host floor without the filter.
**`validate:` cannot be used on the filter** — AppArmor confines `rsyslogd` to
`/etc/rsyslog.conf` and `/etc/rsyslog.d/**`, and Ansible validates a candidate in a temp
directory, so the config is checked in place afterwards and the write undone on failure.

**What the cap costs a forensic read, and how to read a boot in spite of it.** Priority-info
lines are gone from the journal, and from `/var/log/syslog` for every facility except `kern`,
`auth`, `authpriv` and `mail`. That set includes k3s's own output, every `Starting` and `Started`
unit line, and timesyncd's `Initial clock synchronization` step. An empty `journalctl -u k3s`
window therefore means *nothing at notice or above*, not *nothing happened*; the systemd
`Failed with result` lines are the only trace of a crash loop in the journal. The cause is in
`/var/log/k3s.log` since #1918: the k3s role's `tasks/unit-logging.yml` sends the unit's stdout
and stderr there through a drop-in, because every k3s line is priority info and this cap dropped
all of them — on 2026-09-09 k3s crash-looped about 3000 times over five hours and left no reason
behind. Kernel info lines DO survive, in `/var/log/syslog` only: `NIC Link is Up`, veth and cni0
bridge events, `PM: suspend entry`. A read that finds nothing in the journal is not finished
until it has grepped syslog.

**The second trap is the clock.** daniel-box's RTC is dead (`PM: RTC time: 00:03:09, date:
2024-01-01` at every boot), so a boot with no network runs on the clock systemd restores from
`/var/lib/systemd/timesync/clock` — the *previous* shutdown's time — until the first NTP step,
and that step is itself an info line nobody stores. Detect it from the journal's
`__MONOTONIC_TIMESTAMP` against `__REALTIME_TIMESTAMP`: a jump in their difference is a step, and
every wall stamp before it is early by that amount. Issue #1804 is the worked case — the box ran
5h08m with the wall clock 65 min slow, so the same event read as "boot 13:57" in the journal and
"boot 15:02" from monotonic, and the first `Link is Up` of the boot read as a mid-life iSCSI
reset.

## The iGPU gate

The `igpu` tag writes `/etc/modprobe.d/i915.conf` (`options i915 enable_guc=2`) and rebuilds the
initramfs, but **never reboots** — unlike the Pi's `Reboot Pi` handler, this fires on the machine
running the whole homelab, so it only prints a reboot reminder.

It is gated on `has_igpu` AND `/sys/module/i915` existing. `has_igpu` alone is the wrong gate: it
means it passes `/dev/dri` through to jellyfin and tdarr, which is vendor-neutral, since AMD does
VAAPI through the same node, while `enable_guc` is Intel-only. The driver check keeps this
correct on a future AMD or NVIDIA host without hostname-gating it.

## See also

- `ansible/roles/setup/initial_setup/CLAUDE.md` — the rules, the granular tag inventory and the
  two autonomous-role contracts.
- [Pi host tuning record](pi-host-tuning-record.md) — the Pi's own tuning, including the
  `has_rsyslog: false` mask this role's filter block reads.
