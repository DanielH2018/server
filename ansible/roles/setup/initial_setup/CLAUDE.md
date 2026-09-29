# initial_setup — host baseline + security hardening

The big host-bring-up role: base packages, per-user Python tooling, SSH/firewall/kernel
hardening, auditing, and file-integrity monitoring. **Not a container role** — a host-setup
role under `ansible/roles/setup/`, run by `initial_setup.yml`, not `deploy.yml`. See repo-root
`CLAUDE.md` for conventions. This is the largest and most fragile setup role — **`--check`
first** and scope with `--tags` when iterating.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's tasks, timer templates, defaults or playbook entry, or a schedule var in group_vars/all.yml. -->
- **Applied by:** `initial_setup.yml --tags "initial_setup"`
- **Crons (14):**
  - `Weekly apt autoremove` — `0 2 * * 0`
  - `Weekly dpkg purge orphaned configs` — `15 2 * * 0`
  - `Weekly secret rotation (auto tier)` — `0 9 * * 0`
  - `Weekly system restart` — `30 7 * * 0`
  - `Weekly firmware update` — `0 7 * * 0`
  - `Clean unused Docker images` — `30 6 * * *`
  - `Clear ansible log file` — `0 6 * * 0`
  - `Weekly git object-store repair` — `20 4 * * 0`
  - `Refresh homelab infrastructure map` — `*/15 * * * *`
  - `TLS cert-expiry watch` — `10 5 * * *`
  - `Refresh generated docs` — `17 6,18 * * *`
  - `Homelab eval sweep` — `0 2 * * 0`
  - `Weekly rkhunter malware scan` — `0 2 * * 3`
  - `Weekly AIDE file integrity check` — `0 3 * * 1`
- **Timers (3):** `kuma-check-secret-rotation-audit.timer` (`OnCalendar=*-*-* 08:00:00`),
  `kuma-check-setup-drift.timer` (`OnCalendar=*-*-* 07:50:00`),
  `kuma-check-loki-read-route.timer` (`OnCalendar=*-*-* *:23:00`)
<!-- /generated_from -->

## Where it runs
- In `ansible/initial_setup.yml`, after [[config_files]] and before [[sops_setup]] /
  [[docker_install]] — **every host** (Pi-specific tasks self-guard, see below).
- `uv run ansible-playbook ansible/initial_setup.yml --tags "initial_setup"`.

## Hardware gates are capability flags, not host names
A task that runs on one machine gates on a flag naming the HARDWARE fact it reads —
`ansible/inventory/group_vars/all.yml:has_low_memory_board`,
`ansible/inventory/group_vars/all.yml:has_raspi_kernel`,
`ansible/inventory/group_vars/all.yml:has_ample_ram`. Each defaults false in `group_vars/all.yml`
and is true in one host's `host_vars`, so replacing a machine is a `host_vars` edit. They sit in
`group_vars` because `scripts/deploy_tools/land_reach.py:_eval_when` resolves a gate against
`group_vars` + `host_vars` only, and an unresolvable name reads as every host.

Three gates keep a host literal behind a `DECIDED:` comment: the CPU-governor cleanup, the
LXD-snap debloat and the stale WireGuard UFW rule. They read HOST HISTORY, which no capability
names, and they delete themselves once the host converges.
`ansible/tests/setup/test_initial_setup_host_gates.py::test_a_surviving_host_literal_is_one_of_the_recorded_deliberate_ones`
refuses a new bare literal.

## Granular tags (run one block without the whole role)
Every task carries a block tag (placed right under `name:`), so e.g.
`--tags fail2ban` or `--tags "ssh,firewall"` runs just that slice:
`pi-swap` (Pi swapfile + watchdog-stop preamble) · `apt-upgrade` (the Docker-engine hold, then the full dist-upgrade — [[docker_install]]'s CLAUDE.md has why the hold sits here)
· `packages` · `tooling` (uv + CLI tools) · `unattended-upgrades` · `sudo-timestamp` · `fail2ban` · `ssh`
· `crons` (restart / prune / log-truncate / autoremove / dpkg-purge / infra-map; the prune cron
also answers to `prune`, the daniel-box-only infrastructure-map refresh to `infra-map`, the
fwupd-gated weekly firmware update to `firmware`, and the Loki read-route witness to
`loki_route_witness`)
· `journald` · `tuning` (server CPU governor + swappiness) · `debloat`
(server LXD-snap removal + both-hosts networkd-dispatcher mask) · `git-hooks` · `sysctl` · `firewall` (UFW) · `audit` ·
`file-perms` · `kernel-modules` (blacklist + wireguard) · `igpu` (i915 GuC for QuickSync,
`has_igpu` hosts only) · `accounting` (sysstat + acct) ·
`banners` · `rkhunter` · `login-defs` · `coredumps` · `postfix` · `aide`.
**Fact-dependency rule:** a task whose `register:` feeds other blocks carries ALL its
consumers' tags (e.g. the home-dir resolver is `[tooling, git-hooks]`) — keep that
invariant when adding tasks, or tag-scoped runs die on undefined variables.

## What it does (`tasks/main.yml`, grouped)

`tasks/` is the inventory: one file per block tag, named for its subject, and the tag list
above says which file a tag runs. Read the file for what a block does. What follows is only
what a task file does not say on its own — the rules and traps an operator needs before
editing one.

- **Every `tooling` and `git-hooks` task is gated on `dev_tooling_hosts`** (daniel-box and
  daniel-server). daniel-pi holds a checkout but nobody commits from it, and the tag would put
  uv and a pinned Python on a 512 MB Zero 2 W (#1726). Membership is enforced by
  `ansible/tests/setup/test_dev_tooling_hosts.py`.
- **Every tool this role installs is pinned, and a bump needs its digest regenerated** (#2148).
  uv's version, installer URL and sha256 sit together at
  `ansible/roles/setup/initial_setup/defaults/main.yml:initial_setup_uv_version`, so
  `scripts/validate/asset_pins.py` can render the URL and produce the hash an unattended
  renovate_agent run has no `curl` to produce — `--only initial_setup_uv_installer` (#2301). `ansible-core`,
  `ansible-lint`, `prek` and Vale each equal a twin in `pyproject.toml`, `prek.toml` or CI
  (`ansible/tests/repo/test_host_tool_pins_match_their_twins.py`,
  `ansible/tests/repo/test_vale_matches_the_ci_pin.py`), and every install is version-gated
  rather than `creates:`-gated, so a bump replaces the tool.
- **This play becomes root, so `~` is `/root` — SSH paths are absolute only** (#2413, ratcheted
  by `ansible/tests/setup/test_ssh_dir_paths_are_absolute.py`). The home-dir resolver task
  exists for the same reason, and its `register:` is why it carries every consumer's tag.
- **The `Match sys_user` block sets `AllowTcpForwarding all`, not `local`** (#397), plus
  `AllowStreamLocalForwarding remote` and `StreamLocalBindUnlink yes`, for the clipboard
  bridge's reverse unix-socket forward: sshd builds the channel layer from
  `AllowTcpForwarding` alone, so `local` refuses every remote forward before
  `AllowStreamLocalForwarding` is read. The comment above the block in `tasks/access.yml`
  carries the derivation and the log line that tells the two refusals apart.
- **The UFW LAN allow must sort above the SSH limit rule**, which is why its task declares
  `insert: 0` / `insert_relative_to: first-ipv4`: ufw takes the first matching rule, so an
  allow appended below the limiter is inert (#508).
  `ansible/tests/setup/test_ssh_rate_limit_lan_exempt.py` pins the exemption and its ordering.
  The exemption exists because `limit` REJECTs a source at 6 connections in 30 seconds on a
  rolling window, counting SUCCESSFUL ones, so retries sustain the block — parallel Claude
  sessions over ssh crossed it twice. fail2ban's sshd jail is what answers a credential attack.
- **UFW owns a SECOND sysctl file**, `/etc/ufw/sysctl.conf` (`IPT_SYSCTL` in
  `/etc/default/ufw`), re-applied on every `ufw enable`/`reload`. Ubuntu ships it with
  `log_martians=0`, so before #2977 this role's loop wrote 1 and `Enable UFW firewall` wrote 0
  back twenty tasks later. **A sysctl this role sets that UFW's file also names must be set
  there too**, or the run ends with UFW's value live.
- **Unattended upgrades: two traps, both of which look correct until they matter.** Use
  `Origins-Pattern`, not `Allowed-Origins` — the legacy form is rewritten to `o=X,a=Y` and
  matches only a repo publishing a `Suite:` field, which the gh repo does not
  (`ansible/tests/setup/test_unattended_origins_pattern.py`). And use the `::` append syntax: a
  `{ ... }` block reads as a replacement and drops the `-security` and ESM pockets. Verify both
  `apt-config dump` keys — `Allowed-Origins` must still list `-security`, `Origins-Pattern` the
  extras from `unattended_upgrades_origins_patterns`, which is `[]` for security-only.
- **The credential cache is what stops every `!` command re-prompting.**
  `/etc/sudoers.d/10-timestamp` sets `timestamp_type=global` with a 60-minute timeout, so one
  authentication covers every pane and every tty-less shell. It is written with
  `validate: visudo -cf %s`, because a malformed drop-in locks the escalation path out, and
  that path is the only write path to the cluster.
- **AIDE and rkhunter are scheduled against each other, and the package's own timer is
  masked.** `dailyaidecheck.timer` duplicated the weekly cron nightly with broken mail
  alerting, at about 1h20m of CPU per night on the Pi. Both weekly scans run
  `nice -n19 ionice -c3`, staggered to AIDE Monday 03:00 and rkhunter Wednesday 02:00, because
  they used to overlap at full priority for over an hour each on the Pi. The AIDE database
  init is the slow part of a first run on any host.
- **Postfix binds `inet_interfaces = loopback-only`** and needs `notify: Restart Postfix`: a
  reload does not rebind the socket, so the `0.0.0.0:25` listener would survive it.
- **The Pi's swap file is bring-up, not tuning.** It is gated on `has_low_memory_board` and the
  hardware watchdog is stopped around it, because heavy apt on a 512 MB Zero 2 W OOMs without
  disk swap. Pi-only packages gate on `has_raspi_kernel` instead — the board and its memory are
  two separate facts.

## Setup-plane drift reader (`setup_drift` tag)
`setup-drift-check.sh` answers two questions daily on the hosts named in
`setup_drift_check_hosts` (`group_vars/all.yml`): is the `copy:`-deployed code here identical to
its repo source, and has any `template:`-rendered setup script's SOURCE changed since this host
rendered it?

It runs from `kuma-check-setup-drift.timer` since 2026-09-19, and so does the daily
secret-rotation audit (`kuma-check-secret-rotation-audit.timer`, gitops host only): both
import [[common]]'s `kuma_check_timer.yml`, exit 1 after a down push, and rerun every 30 min
under `Restart=on-failure` until they exit 0. For the audit that includes the exec'd
`secret_rotation.py audit --push`, which returns 1 when it pushed `down`. Each import's
`kuma_check_state` follows the host gate, so a host dropped from the list, or one without
`has_gitops`, loses the timer and the cron it replaced.

**Why it exists separately from `manifest-prune-check.sh`.** That check answers the same two
questions plus an orphaned-cluster-object arm, but it is installed by
`roles/setup/k3s/tasks/health-crons.yml`, imported only from that role's `main.yml`, which
`k3s-bringup.yml` asserts onto `k3s_server_hosts`. So it exists on daniel-box and nowhere else,
while daniel-server renders the entire UPS shutdown chain ([[nut_host]]). Confirmed live
2026-08-29: daniel-server had no `/var/lib/homelab/setup-render-manifest.d` at all, and its
`/etc/nut/upsmon.conf` was dated Aug 17 against a template changed on 2026-08-28 (review M-10).

- **The two arms are not reimplemented** — both readers source `files/setup-drift-lib.sh`, copied
  to `/usr/local/lib` like `kuma-push-lib.sh`. Two copies of a drift check are free to drift from
  each other, which is the fault a drift check reports.
- **It does NOT carry the orphan arm.** That needs `/etc/rancher/k3s/manifests` and the control
  plane's staged set; an agent node has neither, and an arm that structurally cannot fire reads
  as coverage.
- **A third arm reports the checkout's age**, because the render arm compares a render against
  the tree on THIS host and a host without gitops-deploy does not refresh that tree —
  daniel-server was measured 39 commits behind origin on 2026-08-17. A stale checkout makes the
  stamp and the template agree, so the arm would read green exactly when the host is furthest
  behind. Past `setup_drift_tree_max_days` (14) the age is its own DOWN; an unreadable checkout
  is a DOWN too, never a pass.

  **That third arm reads the tree with `git -c safe.directory="$REPO_DIR"`, and the exception is
  load-bearing.** The cron runs as root against a checkout owned by `sys_user`, which is exactly
  git's dubious-ownership case, so a bare `git log` exits non-zero and the arm reports "cannot
  read the checkout" on every run. It did: the tile was DOWN from the arm's first cron run
  (2026-08-30 07:50) until this fix. The 2026-08-29 hand-run that verified the arm ran as
  `ubuntu` and passed, which is the whole shape of the miss — **verify a cron arm as the user
  cron runs it, not as yourself.** `ansible/tests/deploy/test_setup_drift_check.py` now forces the
  refusal with `GIT_TEST_ASSUME_DIFFERENT_OWNER=1`, and carries a control asserting a bare read
  still fails under it, so the accept half cannot pass because the simulation went inert.
- Pushes the "Setup Plane Drift (agent hosts)" Kuma tile. Its token is in
  `CROSS_HOST_PUSH_TOKENS`: the cron is on daniel-server and the tile deploys from daniel-box, so
  no single `rotate --deploy` can move both halves.

Guarded by `ansible/tests/deploy/test_setup_drift_check.py`, which EXECUTES the library against a
fixture rather than grepping it, and by `ansible/tests/deploy/test_setup_render_manifest.py`, whose
guards now point at the library plus one that both consumers still source it.

## Loki read-route witness (`loki_route_witness` tag)
`loki-read-route-health.sh` runs hourly on every host in `loki_route_witness_hosts`
(`group_vars/all.yml` — both prod cluster nodes) and pushes that host's own "Loki Read Route
(<host>)" Kuma tile.

**It is a systemd timer, `kuma-check-loki-read-route.timer`, not a cron, since 2026-09-19.**
The `Schedule the Loki read-route witness` task imports [[common]]'s `kuma_check_timer.yml`,
whose service is `Restart=on-failure` with `RestartSec=15min`: the script exits 1 after it
pushes `down`, so a red tile reruns every 15 minutes until the route answers instead of
sitting red until the next hourly slot. The timer is `Persistent=true`, so a slot missed inside
an outage runs at boot; the script's boot-grace arm exits 1 without a push, and the restart
carries the real verdict. `systemctl status kuma-check-loki-read-route` shows `auto-restart`
while it is red, and `journalctl -u kuma-check-loki-read-route` holds the runs. The import
removes the old `Loki read-route witness` cron on every host.

**Why it is a host check and not a monitor-bridge check.** loki-homelab's read route is guarded by
a ClientIP set of node-owned addresses, and its only caller is `probe.py loki-query` /
`loki-labels` running as a host process. A pod prober arrives with a pod IP, so it either fails
for a reason the operator never hits or has to be granted an address no real caller uses.

**Why both nodes.** Which address a host process arrives as depends on where the traefik pod
sits — same node gives that node's cni0 gateway, another node gives the sender's flannel.1
address. #1693 left the route dead from daniel-server and healthy from daniel-box, so a single
witness would have reported green throughout. Each host pushes its own token, for the reason
[[nut_host]]'s secondary watchdog records: two hosts on one token let either host's `up` satisfy
the deadline.

**The verdict is the response body, never the exit code.** `probe.py` builds its curl argv
without `-f`, so a route Traefik refuses returns `404 page not found` with exit **0**.
`files/loki_route_health.py` decides — valid JSON, `status: success`, non-empty `data` — and
`ansible/roles/setup/initial_setup/tests/test_loki_route_health.py` holds that 404 as a DOWN case.
`ansible/tests/setup/test_loki_route_witness.py` guards the host list, the per-host tokens, the
push deadline against the timer period, the exit-code contract (a down verdict exits 1), and
the removal arm.

Both tokens are in `CROSS_HOST_PUSH_TOKENS` (`scripts/secrets_mgmt/consumers.py`): the timer is
here, in a role with no deploy tag, and the tile deploys from `k8s/uptime-kuma`, so no single
`rotate --deploy` can move both halves.

## Autonomous-role contract — Generated docs refresh (`crons` tag)
Twice daily (06:17 and 18:17, daniel-box only): `docs-refresh.sh.j2` regenerates
`docs/reference/` and the infra map, records a weight for every test module the shard table
lacks, rebuilds the MkDocs site, and publishes any diff through a PR that auto-merges. Its own
header carries the derivations; this is the authority statement a later edit must not widen
quietly.
- **Scope:** three derived paths, and nothing else — `docs/reference/`,
  `docs/assets/generated/`, and `scripts/dev/pytest_shard_weights.json`. Each is reproducible
  from the tree by a generator whose output is itself tested, which is what makes a review-free
  auto-merge acceptable. A generator that writes outside the three is a defect: the write is
  unstaged, and an unstaged file in the primary checkout parks every deploy on the box.
- **Why it weights test modules at all (#2274).** CI's `pytest_shard.py --check-durations` step
  rejects the PR that introduces an unweighted module costing `RUNNER_HEAVY_SECONDS` or more
  and leaves the repair to a human. It says nothing about a lighter one, and an unweighted file
  is packed at the suite median of 0.0s — so without the cron the split is un-skewed rather
  than measured. `--record-missing`, never `--record`: it measures only the files the table
  lacks, and writes the same bytes back when there are none, so an ordinary run leaves no diff.
- **Mode:** degrade, never abort. A failing generator and a failing measurement both set
  `GENERATORS_OK=0` and let the run publish what did succeed, then report the push DOWN with
  the reason named — the same "act, but don't launder the result" split the eval sweep uses
  below. **A red suite is not a case degradation saves**: the script runs the
  suite before its commit and exits 1 on a failure. The prek `pytest` hook that
  did this fires at pre-push (#2827). What it saves is the suite being fine and the measurement alone failing: the
  `timeout` firing, or a durations report that parses to nothing. The `timeout` is there because
  this script holds the git-tree lock for its whole life.
- **Abort valves:** the shared `/var/lock/server-git-tree.lock`; the dirty-tree gate at the top
  (build the site, change no file); the unlanded-branch guard (`publish_pr.py unlanded`, read
  against origin rather than the open-PR list); the commit-failure stamp under
  `/var/lib/homelab/docs-refresh.d`, which is what lets the next run tell its own leftover dirt
  from a human's.
- **Required evidence:** every run logs `status=<up|down> <msg>` via `logger -t docs-refresh`
  and pushes the "Docs Refresh" Kuma monitor. `PUSH_STATUS` defaults to `down`, so a path added
  later that forgets to set it reports a failure rather than a silent success.
- **Next-run review:** before adding a fourth staged path, or a prek hook matching one of the
  three, check the hook cannot fail on generator output — a hook that can wedges this cron on
  every run, and `end-of-file-fixer` and Vale have both already tried.

## Autonomous-role contract — Homelab eval sweep (`evals` tag)
Weekly (Sunday 02:00, daniel-box only): grades every case under `evals/cases/` against the
homelab agents/skills and rolls the result into `evals/history.json` (`evals/trend.py`), then
publishes it. Written down for the same reason as autofix-bridge's contract — a
change-producing cron with no human in the loop needs its authority stated so a later edit
can't quietly widen it.
- **Scope:** run the existing eval cases and commit the trended result. Never edits a case,
  never touches an agent/skill definition, never runs the `run-live.mjs` live-smoke path.
- **Mode:** binary, no dial. Either `anthropic_api_key` exists and the sweep runs hermetic, or
  it is empty and the whole run is a no-op that reports UP with the reason logged
  (`eval-run.sh.j2`'s header) — there is no non-hermetic fallback, because a subscription run's
  numbers are noise `evals/trend.py` refuses to trend anyway.
- **Authoritative source:** the chezmoi eval engine's own grading
  (`~/.local/share/chezmoi/evals/run-evals.mjs`) — never a cached or hand-edited report.
- **Abort valves:** the shared `/var/lock/server-git-tree.lock` (a dirty tree or another
  in-flight commit means the sweep's result is thrown away, not recorded against a tree it
  didn't grade); the unlanded-branch guard (`publish_pr.py unlanded` — an `evals-history/*`
  branch still on origin skips the run rather than stacking a second one, and it reads origin
  rather than the open-PR list because a failed `gh pr create` leaves a branch with no PR);
  the empty-key gate above.
- **Required evidence:** every run logs `status=<up|down> <msg>` via `logger -t eval-run`
  (readable in Loki) and, when armed, pushes the Kuma "Homelab Evals" monitor. A REGRESSED case
  still gets committed (the data is real) but reports the push DOWN with the regression named,
  matching autofix-bridge's "act, but don't launder the result" pattern rather than either
  silently dropping the regression or silently blocking the commit.
- **Next-run review:** before widening scope (grading `run-live.mjs`, changing cadence, adding
  a case dir the loop should skip), read the last few PRs this cron opened
  (`evals-history/*`, squash-merged) for what actually regressed or flaked.

## Notable
- **Handlers live in the playbook, not this role** (there is no `handlers/main.yml`) — `Restart
  SSH`, `Restart fail2ban`, `Reload audit rules`, `Reload Postfix`, `Restart Postfix`, `Restart
  rsyslog`, `Restart systemd-journald`, `Rebuild initramfs for i915`, `Warn that a reboot is
  required for i915 GuC` are defined in `initial_setup.yml`. Same pattern as [[optimize_pi]]: a
  new `notify:` here needs a matching handler added to that playbook.
  **`Restart rsyslog` is defined ABOVE `Restart systemd-journald` on purpose** — handlers fire
  in definition order, not notify order, and journald begins forwarding at info the moment it
  restarts. ENFORCED by `ansible/tests/setup/test_journald_syslog_forwarding.py`.
- **`journald` tag: the two files are one change.** `50-homelab.conf` caps the journal
  (`SystemMaxUse=1G`, `MaxLevelStore=notice`) and sets `MaxLevelSyslog=info`;
  `/etc/rsyslog.d/49-homelab-info-filter.conf` then discards info for every facility that
  reaches rsyslog *through* journald. Deploying either alone is a defect, so
  `ansible/tests/setup/test_journald_syslog_forwarding.py` pins them together.
  **Except where there is no rsyslog to pair with.** The filter block is `when: has_rsyslog`
  (`group_vars/all.yml`, default true; `host_vars/daniel-pi.yml` sets it false because
  [[optimize_pi]] masks rsyslog there). A masked unit fails the `Restart rsyslog` handler, and
  the printed Pi remediation for PR #1942 ended `failed=1` with every change live (#1946).
  `MaxLevelSyslog=info` stays unconditional on the Pi: with no rsyslog listening, journald's
  forwarding reaches nothing. Same test file, `test_the_filter_is_gated_on_has_rsyslog`.

  **Why they differ.** journald owns `/dev/log`, so rsyslog only ever sees what journald
  forwards, and `Store` and `Syslog` are applied to their sinks independently. `MaxLevelSyslog`
  read `notice` from 2026-08-01 to 2026-08-29 and deleted the host's SSH authentication trail:
  `Accepted publickey ... SHA256:` and every `pam_unix(...:session)` line is priority info.
  sshd's `LogLevel VERBOSE` was correct throughout and irrelevant — an application's log level
  bounds what it emits, not what survives. Two things depended on those lines: which key
  authenticated is recorded nowhere else (auditd has the login, never the fingerprint), and
  the crowdsec node agent tails `auth.log` for the SSH brute-force signal.

  **The filter's exemptions are load-bearing.** `auth`/`authpriv` carry the records; `kern` and
  `mail` must be exempt because they do *not* arrive via journald — rsyslog reads kernel
  messages itself through `imklog`, and postfix writes to its own listen socket — so discarding
  their info removes lines from `kern.log` and `mail.log` that are present today.
  Measured cost of the pair: +0.34 MB/day/host, against a +7 MB/day/host floor without the
  filter. **`validate:` cannot be used on the filter** — AppArmor confines `rsyslogd` to
  `/etc/rsyslog.conf` and `/etc/rsyslog.d/**`, and Ansible validates a candidate in a temp
  directory, so the config is checked in place afterwards and the write undone on failure.

  **What the cap costs a forensic read, and how to read a boot in spite of it.** Priority-info
  lines are gone from the journal, and from `/var/log/syslog` for every facility except
  `kern`, `auth`, `authpriv` and `mail`. That set includes k3s's own output, every
  `Starting`/`Started` unit line, and timesyncd's `Initial clock synchronization` step. An
  empty `journalctl -u k3s` window therefore means *nothing at notice or above*, not
  *nothing happened*; the systemd `Failed with result` lines are the only trace of a crash
  loop in the journal. The cause is in `/var/log/k3s.log` since #1918: the k3s role's
  `tasks/unit-logging.yml` sends the unit's stdout and stderr there through a drop-in,
  because every k3s line is priority info and this cap dropped all of them — on 2026-09-09
  k3s crash-looped ~3000 times over five hours and left no reason behind. Kernel info lines DO survive, in
  `/var/log/syslog` only: `NIC Link is Up`, veth/cni0 bridge events, `PM: suspend entry`. A
  read that finds nothing in the journal is not finished until it has grepped syslog.
  The second trap is the clock. daniel-box's RTC is dead (`PM: RTC time: 00:03:09, date:
  2024-01-01` at every boot), so a boot with no network runs on the clock systemd restores
  from `/var/lib/systemd/timesync/clock` — the *previous* shutdown's time — until the first NTP
  step, and that step is itself an info line nobody stores. Detect it from the journal's
  `__MONOTONIC_TIMESTAMP` against `__REALTIME_TIMESTAMP`: a jump in their difference is a
  step, and every wall stamp before it is early by that amount. Issue #1804 is the worked
  case: the box ran 5h08m with the wall clock 65 min slow, so the same event read as
  "boot 13:57" in the journal and "boot 15:02" from monotonic, and the first `Link is Up`
  of the boot read as a mid-life iSCSI reset.
- **iGPU (`igpu` tag)** writes `/etc/modprobe.d/i915.conf` (`options i915 enable_guc=2`) and
  rebuilds the initramfs, but **never reboots** — unlike `Reboot Pi`, this fires on the machine
  running the whole homelab, so it only prints a reboot reminder.
  **Gated on `has_igpu` AND `/sys/module/i915` existing.** `has_igpu` alone is the wrong gate:
  it means "pass `/dev/dri` through to jellyfin/tdarr", which is vendor-neutral (AMD does VAAPI
  through the same node), while `enable_guc` is Intel-only. The driver check keeps this correct
  on a future AMD/NVIDIA host without hostname-gating it — deliberately, since the repo is
  removing `daniel-server` literals ahead of the master-node migration.
- **`become` vs HOME:** the `Resolve the deploy user's home directory` task exists because
  `ansible_facts.env.HOME` is root's under the play's `become: true`, but uv / per-user tooling
  must install for the unprivileged deploy user — recent fixes (Pi bring-up era) replaced naive
  `env.HOME` refs with this resolver. Keep new per-user tasks using it, not `env.HOME`.
- **AIDE DB init is slow (~8 min)** and runs with progress monitoring — expect a long pause on
  first run / fresh host; not a hang.
- **Templates:** `templates/98_aide_local.conf.j2` (AIDE exclusions for the Docker homelab) and
  `templates/fail2ban_homelab.conf.j2` (the fail2ban jail).
- Pi-only tasks are individually `when:`-guarded rather than block-scoped, so a server run
  simply skips them.
