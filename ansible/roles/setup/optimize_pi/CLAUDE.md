# optimize_pi — Raspberry Pi host tuning

Low-level OS, hardware and resolver tuning for the Pi. **Not a container role** — this is a
host-setup role under `ansible/roles/setup/`, run by `initial_setup.yml`, not `deploy.yml`.
See repo-root `CLAUDE.md` for conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's tasks, timer templates, defaults or playbook entry, or a schedule var in group_vars/all.yml. -->
- **Applied by:** `initial_setup.yml --tags "optimize_pi"` when `inventory_hostname ==
  'daniel-pi'`
- **Crons (3):**
  - `Pi SD-card health heartbeat` — `*/5 * * * *`
  - `Pi rotated-log integrity sweep` — `40 1 * * *`
  - `Pi container-recovery heartbeat` — `*/5 * * * *`
<!-- /generated_from -->

## Where it runs
- Invoked from `ansible/initial_setup.yml`:
  `{ role: optimize_pi, tags: ["optimize_pi"], when: inventory_hostname == 'daniel-pi' }`
  — **Pi only** (guarded by `inventory_hostname`).
- Run it with: `uv run ansible-playbook ansible/initial_setup.yml --tags "optimize_pi" -e target=daniel-pi`
  (use `--check` first — several changes trigger a reboot). NB `-e target=`, not `--limit`:
  the play's `hosts:` defaults to the local hostname, so `--limit daniel-pi` from the
  server intersects to zero hosts and silently does nothing.
- **Granular tags** (one section without the whole role): `gpu-mem`, `zram`, `log2ram`,
  `watchdog`, `debloat`, `earlyoom`, `apt-timers`, `node-exporter-host`, `sd-health`,
  `recovery-health`, `gz-integrity`, `pi-dns`. The shared prep
  tasks are dual-tagged (`Set variables` →
  `[gpu-mem, zram]`; the config.txt path detection → `[gpu-mem, watchdog]`) so
  tag-scoped runs still get the facts they consume. `log2ram` also covers the log
  RAM-budget tasks (journald cap, acct retention, the auditd cap and its rotation
  cleanup, sysstat retention) — they exist because of the tmpfs. `sd-health` and
  `recovery-health` each also create `/var/log/pi-health` and its logrotate stanza, so
  either tag alone leaves the cron it installs able to write its verdict. Everything the
  rotated-log sweep needs carries BOTH `sd-health` and `gz-integrity`, because the SD-card
  heartbeat reads that sweep's verdict and treats a missing one as a `down` — so a
  `--tags sd-health` run has to install the sweep, prime its verdict and schedule it too.
- **The two health crons source `/usr/local/lib/kuma-push-lib.sh`**, installed by the
  `initial_setup` role under `tags: [always]` so a `--tags sd-health` or `--tags recovery-health`
  run still copies it. Both scripts guard the `source` with `|| exit 1`, so a host missing the
  lib drops its heartbeat loudly instead of reporting green.

## What it does (`tasks/main.yml`)
1. **Config path detection** — picks `/boot/firmware/config.txt` (Bookworm) vs
   `/boot/config.txt` (Bullseye).
2. **GPU memory split** — `gpu_mem=16` to reclaim RAM (headless).
3. **ZRAM** — installs `zram-tools`; `PERCENT=100` + `ALGO=zstd` (100% is uncompressed
   *capacity* ~456 MB, raised from 75% on 2026-09-18 when the device was 77% full and 31 MB had spilled to the SD swapfile; the task comment has the numbers).
   **Do not shrink `PERCENT` to reclaim RAM** — the arithmetic runs the other way. Measured
   2026-08-29 from `/sys/block/zram0/mm_stat`: 275.7 MiB stored in 62.4 MiB of physical RAM,
   a **4.61:1** ratio (better than the ~3.5:1 this line assumed), with `mem_used_max` 76.6 MiB
   as the historical worst case. It is the cheapest RAM on the box, and pages it cannot hold
   go to the SD-card swapfile at roughly 1000× the latency.
   Plus zram-aware VM sysctls: `vm.swappiness=130` (zram swap is cheaper than evicting
   hot page cache — the server runs 10 for the opposite reason) and `vm.page-cluster=0`
   (no readahead on random-access zram; ~8× lower swap-in latency).
4. **Log2Ram** — adds the Azlux repo + installs `log2ram` to spare the SD card from log writes.
   **Then patches `--sparse` out of both of Log2Ram's `rsync` command lines**, because
   `--sparse` with `--inplace` corrupts any file whose content ends in NUL bytes. rsync seeks
   over the source's NUL run rather than writing it, and `--inplace` writes into the live
   destination, so the previous file's bytes survive at those offsets. That clobbered the
   trailing NULs of the gzip ISIZE field in 6 of 9 rotated
   `/var/log/apt/history.log.*.gz` on daniel-pi (`gzip -t` → "invalid compressed data--length
   error", #2694). **The log content was never lost** — the CRC32 matched in every case, so
   `zcat` recovers every byte, and only `gzip -t` and `zgrep`'s exit status disagree. Which
   files survive is a coin flip on what the previous file left at those offsets, so a clean
   `gzip -t` proves nothing about the sync.
   **Do not "simplify" this to `USE_RSYNC=false`.** That conf knob takes Log2Ram's
   `cp -rfup --sparse=always` fallback, which is byte-correct but has no `--delete`: files
   deleted from the tmpfs would linger on the SD card and `sync_from_disk` would restore them
   into the 128 MB `/var/log` at boot. The task carries a `# DECIDED:` marker with the byte
   evidence and the rsync version it was reproduced under.
   **The patch is forward-only.** Repairing the six already-corrupt files needs a privileged
   write on the Pi; they are readable as they stand and age out as they rotate.
5. **Hardware watchdog** — `dtparam=watchdog=on` + `watchdog` daemon, auto-reboot if 1-min
   load > 24.
6. **Debloat** — purges Open vSwitch (was installed but had no bridges/netplan config,
   yet mlockall-pinned ~14 MB) and snapd (zero snaps installed). Verified dependency-safe;
   netplan only Suggests OVS. Also purges fwupd: its hourly `fwupd-refresh.timer`
   swap-thrashed the 512 MB board (healthcheck-timeout storms → autoheal restart loops)
   while never surviving its own 25 s dbus activation timeout; Pi firmware comes via apt,
   not LVFS. Also **masks rsyslog** (journald is the log of record — fail2ban reads the
   journal; rsyslog was a redundant second logger writing text logs into the RAM-backed
   /var/log) and its stale text logs, **masks wpa_supplicant** (the Pi is on wired ethernet,
   `wlan0` DOWN), and removes the stale ~9 MB `aideinit` log (`/var/log/aide/aide.log`; the
   weekly `aide --check` logs to journald, so that file is dead RAM). Reclaimed ~17 MB
   (2026-07-06). Masks, not purges, for rsyslog/wpa — a package update can't silently re-enable them.
   The mask is why `host_vars/daniel-pi.yml` sets `has_rsyslog: false`: [[initial_setup]]'s
   rsyslog filter block reads that flag and skips, since its `Restart rsyslog` handler fails
   on a masked unit (#1946). Unmasking rsyslog here means flipping that flag too.
7. **Log RAM budget** — `/var/log` is log2ram's 128 MB RAM-backed tmpfs, and the role now
   declares that size rather than inheriting it
   (`ansible/roles/setup/optimize_pi/defaults/main.yml:optimize_pi_log2ram_size`, with
   `LOG_DISK_SIZE`, `ZL2R` and `COMP_ALG` beside it). log2ram 1.7.2 ships exactly those four
   values and daniel-pi's `/etc/log2ram.conf` is byte-identical to upstream's, so the
   declaration writes nothing today — it costs something only when a package default moves,
   which is when the caps below need the number pinned (log2ram shipped `SIZE=40M` before
   128M). #2714 read the live file as hand-edited; it is not, and its duplicate
   `JOURNALD_AWARE=true` is upstream's own bug at 1.7.2, fixed on master.
   **The duplicate line stays.** Both copies read `true`, log2ram sources the file, so the
   last assignment wins and the duplicate is inert — while deleting a shipped line would make
   the conffile dpkg-MODIFIED and manufacture the apt-upgrade conffile prompt #2714 wanted
   gone. What the role checks instead is that the copies AGREE (#2726): a task counts the
   DISTINCT `JOURNALD_AWARE=` values and fails on anything but one, so two lines that disagree
   — where the effective value is silently whichever sits last — stop the play, and a later
   log2ram that ships the line once passes unchanged. ENFORCED by
   `ansible/tests/setup/test_optimize_pi_declares_log2ram_sizes.py`, which runs the task's own
   awk program against an agreeing pair, a disagreeing pair and a file with no such line.
   The caps themselves (was 81% full 2026-06-11): a Pi journald drop-in (`60-homelab-pi.conf`, `SystemMaxUse=32M`) overrides
   initial_setup's server-sized 1G cap, `ACCT_LOGGING="3"` cuts pacct retention from
   30 daily generations (savelog via `/etc/cron.daily/acct`, ~28 MB/day of healthcheck
   exec churn) to 3, auditd is capped to a 12 MB ceiling, and `HISTORY=2` cuts sysstat's
   shipped 7 days (18 sa/sar files, 9.9 MB) to 2. **Every cap here is a cap, never a
   disable** — `sar`, `lastcomm` and `ausearch` each have a triage row in
   `docs/security-tools.md`, so a census that finds no machine consumer has not found that
   nothing reads them.
   **These trims pay twice.** `Shmem` is ~2.6 MB against 75 MB of content in the tmpfs, so
   most of /var/log is itself swapped into zram. Freeing a megabyte here frees both the
   compressed physical page *and* a megabyte of zram capacity — and capacity is the axis
   under pressure, since zram runs ~81% full with the SD-card swapfile already carrying
   overflow.
8. **earlyoom** — kills the largest process when avail mem drops under 10%, with `--avoid`
   shielding systemd/sshd/dbus-daemon/dockerd/containerd/watchdog. Before this, the only
   escape from a memory spiral was the hardware watchdog hard-rebooting at load 24 after
   ≥10 min of stall.
   **The swap threshold is deliberately non-binding (`-s 100,100`), and that reverses an
   earlier setting.** It read `-s 10`, intending "both memory AND swap exhausted = a true
   spiral". earlyoom's own help states the rule — "both memory and swap must be below
   minimum for earlyoom to act" — and with 1.37 GB of combined swap (456 MB zram, 350 MB when this was measured, at
   priority 100, a 1 GB SD-card swapfile at -2) the swap half was unreachable. Measured
   2026-08-29 across four hours of earlyoom's own report lines: mem avail never left 30-34%,
   free swap never left 77-79%. **The guard was inert for its entire life**, which is why the
   2026-08-29 autoheal create-failure hit a box with no escape hatch at all. `dbus-daemon` is
   in `--avoid` because Docker's cgroup driver is `systemd`: runc asks systemd over dbus for a
   scope on every container start, so killing it leaves nothing able to start a container.
9. **apt timers pinned to the quiet hour** (`apt-timers`, `optimize_pi_apt_timers`) — a
   drop-in per timer sets `apt-daily.timer` to 04:20 and `apt-daily-upgrade.timer` to 05:20
   UTC, once a day, ±10 min. Ubuntu's default fires the update half twice a day at any hour
   (`6,18:00` + 12 h random), and on this box each firing is a four-minute 60 MB burst:
   measured 2026-09-18 at 15:05Z, memory PSI `full avg10` 45%, load 9.9, an RCU stall
   (#2007). The drop-in clears the packaged schedule with an empty `OnCalendar=` first;
   `ansible/tests/setup/test_pi_apt_timers_pinned.py` requires that line and the window.
   The Pi memory budget review of 2026-09-18 has the host's numbers.
10. **node_exporter as a host unit** (`node-exporter-host`, `optimize_pi_node_exporter_*`) —
    the upstream v1.12.1 arm64 tarball, checksum-pinned, unpacked under `/opt` with
    `/usr/local/bin/node_exporter` a symlink to the pinned version (a bump moves the link,
    which restarts `node_exporter.service`), running as the `node_exporter` system user on
    the LAN IP's port 9100 with the collector flags the container ran, plus a UFW allow
    from `lan_subnet`. It replaced the `node-exporter` container on 2026-09-18 (#2005;
    the role is at `git show 2460d0675fd748e70fcbcde87185371ffd62402b:ansible/roles/containers/archive/node-exporter/`): one containerd shim (~5 MB) fewer on a
    host that keeps 10-25 MB free, and the one container whose host form changes no
    decision. `ansible/tests/setup/test_pi_node_exporter_host_unit.py` holds the unit to
    the archived compose's collector set and the version to the cluster DaemonSet's.
    Retiring the container is by hand (`docker rm -f node-exporter` and its
    `containers/node-exporter/` directory) BEFORE the first apply: the container publishes
    the same IP:port the unit binds.
11. **SD-card health heartbeat** — SD cards have no SMART, so `templates/pi-sd-health.sh.j2`
   (cron, */5) pushes the root fs's ext4 `errors_count` to the static "Daniel Pi SD
   Health" Kuma push monitor (uptime-kuma role) via the LAN-only Authelia bypass on
   `^/api/push/` (authelia role). Nonzero count = explicit `down`; a dead cron/host
   trips the 600s push watchdog. Token: `pi_sd_health_push_token` in `secrets.yml`.
   **The same push carries the rotated-log integrity verdict** (#2715).
   `templates/pi-gz-integrity.sh.j2` (cron, daily 01:40 UTC) runs `gzip -t` over every
   rotated `.gz` under `/var/log` and `/var/hdd.log` and writes a two-line verdict to
   `defaults/main.yml:optimize_pi_gz_integrity_state_file`; the heartbeat reads that file and
   pushes `down` on a failing verdict, on a verdict older than
   `optimize_pi_gz_integrity_max_age_h`, and on no verdict at all. Three things about it:
   - **`/var/hdd.log` is the load-bearing tree.** `/var/log` is the RAM tmpfs, rewritten from
     RAM on every rotation; `/var/hdd.log` is the copy on the card and the destination of the
     sync that corrupted 37 files in #2694. Running the sweep against the live Pi on 2026-09-27
     found 37 of 144 failing and every one of them under `/var/hdd.log`, none under `/var/log`.
     A `/var/log`-only sweep read green through all of #2694.
   - **It is daily, not `*/5`.** 1.7s wall (1.44s user) over 144 files and 9.7 MB warm, ~13s
     with the SD-side copies cold, measured by running the script on the Pi. At the
     heartbeat's cadence that is ~8 min/day of CPU and ~2.8 GB/day of reads.
   - **A file count below `optimize_pi_gz_integrity_min_files` is itself a `down`.** `gzip -t`
     over no files exits 0, so without the floor a moved root or a logrotate change turns the
     check green forever.
   The verdict lives under `/var/lib`, not `/var/log`: a verdict in the tmpfs is gone after a
   reboot, and the heartbeat would then report a missing verdict for up to a day.
12. **Container-recovery heartbeat** — AutoKuma reads only the SERVER's docker socket, so the
    Pi's containers have no liveness monitor of their own. The two that die silently are
    `autoheal` (restarts unhealthy containers) and `docker-proxy` (the read-only socket
    Alloy's container-log discovery reads): a dead autoheal stops recovering
    Pi containers, and a dead docker-proxy stops this host's container logs reaching Loki
    while Alloy keeps running with zero targets.
    `templates/pi-recovery-health.sh.j2` (cron, */5) watches **every container the host
    deploys** — `containers_list`, plus `docker-proxy-lifecycle` (and `-codeserver` under
    `has_code_server`), which are services inside the docker-proxy role's compose file rather
    than list entries — via `docker ps`, which reads the real socket through the docker group
    (so it still reports docker-proxy's own death), and pushes to the static "Daniel Pi
    Recovery" Kuma push monitor (uptime-kuma role), same LAN-only `^/api/push/` bypass. Any
    one down = explicit `down`; a dead cron/host trips the 600s watchdog. Token:
    `pi_recovery_push_token` in `secrets.yml`. The set was autoheal + docker-proxy alone until
    #1910: the same failed start reaches glances, wg-easy and docker-proxy-lifecycle, and Kuma
    sees only the first two from outside.
    **It also RESTARTS what it finds dead**, and still pushes `down` for that cycle.
    `restart: unless-stopped` covers a container whose process exits, not one whose *create*
    fails at the OCI runtime — the failure this box actually produces under memory pressure.
    On 2026-08-29 autoheal died with "Timeout waiting for systemd to create scope", sat at
    `RestartCount 0`, and stayed down ~50 minutes until a human ran `docker start`. Detection
    was never the gap. Reporting `down` on a cycle that had to intervene is what keeps an
    auto-restart loop visible: pushing `up` after a successful restart would make a container
    crashing every 5 minutes read green forever.
    **The DOWN line names why**, from `docker inspect` read BEFORE the `docker start`:
    `not running: autoheal [status=exited exit=137 finished=<ts> error=<daemon text>]`. The
    restart (or the next deploy's recreate) erases that state, the journal here rotates in
    about a day (`SystemMaxUse=32M`), and `/var/log/pi-health/health.log` rotates daily on
    log2ram — so the cron's own line, shipped to Loki under `job="syslog"`, is the only record
    of a Pi container failure that survives a week (#1912). `State.Error` is flattened to one
    line and capped at 300 chars so it cannot split the record the alert parser reads.
    **When dockerd itself is gone** — `docker version` does not answer, or every watched
    container read `inspect failed` — the reason is the daemon's, so the line carries
    `; dockerd: <newest non-info journalctl -u docker lines>`
    — systemd's "Main process exited" / "Scheduled restart job" and dockerd's own
    `level=error`, capped at 400 chars (#1922). Not `journalctl -p warning`: dockerd's stderr
    lands at journal priority 6 whatever its `level=` says, so a priority filter returns
    nothing. This is the only path a daemon-level failure reaches Loki by — the Pi ships no
    journal, and `roles/containers/alloy/CLAUDE.md` records the RSS measurement behind that.
    ENFORCED by `ansible/tests/setup/test_pi_recovery_restarts_and_reports.py`, which renders and
    runs the script against a stub `docker` and a stub `journalctl`.

13. **Both health crons leave a durable record** at `/var/log/pi-health/health.log`, which the
    Pi's promtail tails as its `pi-health` job under `job="syslog"`. Kuma keeps only current
    state, so without this a DOWN that clears is gone — and `probe.py alerts` reconstructs
    episodes from `{job="syslog"} |= "status=down"`. Two independent gaps kept daniel-pi out
    of that view, and closing either alone would have changed nothing: the crons emitted no
    `status=` token (`kuma-push-lib.sh` calls `logger` only when the *push* fails), and there
    was no path to Loki for it anyway (rsyslog is masked here by §6, and this promtail build
    is a journal stub — verified: no `sd_journal_open`, no libsystemd, and a `journal:` dry
    run yields zero entries). A file plus a static scrape job needs no new daemon, and carries
    ~576 lines/day where the whole journal would be ~38k.
    **The timestamp format is load-bearing**: `_SYSLOG_LINE_RE` wants exactly two
    whitespace-free tokens before the tag, so the scripts emit `date -Is` (one token).
    Traditional syslog format is four tokens and parses as nothing.
    ENFORCED by `ansible/tests/setup/test_pi_health_log_line_shape.py`, which feeds the scripts'
    real output through the real parser.
    Adding this stream meant excluding it from monitor-bridge's Loki file-tail arm
    (`machine!="daniel-pi"` in `LOKI_STREAM`) — a Pi stream under `job="syslog"` would
    otherwise stop a total cluster file-tail outage from ever reaching that arm's zero
    threshold.
    **Two deploys to activate** (Pi is manual-deploy): install the cron with
    `initial_setup.yml --tags recovery-health -e target=daniel-pi` (which also copies the shared
    push lib — see *Granular tags*), then redeploy `uptime-kuma`
    on the server so AutoKuma provisions the monitor — do both close together or the fresh push
    monitor false-DOWNs until the first heartbeat lands.

14. **Resolver** — a static `/etc/resolv.conf` (rendered from the SHARED
    `roles/setup/common/templates/resolv.conf.j2`, the same file daniel-box uses) lists the
    cluster Pi-hole first and a public resolver behind it, and systemd-resolved is disabled.
    Before this the DHCP lease's ISP resolvers answered everything, including every internal
    `*.local.<domain>` name — those resolve publicly via the Cloudflare wildcard, so the
    private hostnames were leaving the LAN.
    **The order is the entire mechanism.** `resolv.conf(5)` queries nameservers "in the order
    listed" and the `rotate` option — deliberately unset — is what would stop clients trying
    "the first listed server first every time". So the Pi falls through to the public resolver
    while Pi-hole is unreachable and returns to it on the very next query, automatically, in
    both directions, with no daemon watching anything.
    **This replaces PR #693's resolved drop-in, which did not work.** systemd-resolved is
    sticky by design and treats its `DNS=` list as interchangeable peers; measured 45 minutes
    after that deploy, `Current DNS Server` read 1.1.1.1 at 15:12 and 10.0.0.243 at 15:14,
    unprompted. Disabling resolved also removes the LINK scope a drop-in could not reach —
    `resolvectl status` still listed 75.75.75.75, 75.75.76.76 and two Comcast v6 addresses
    under Link 2, with their own `Current DNS Server`.
    **It also moves the Pi's containers, but only after a restart.** Docker's embedded
    resolver pins its upstreams at container START, so changing the host's resolv.conf leaves
    every running container forwarding to whatever was there before. On the 2026-09-01
    cutover all seven still carried `ExtServers: [host(127.0.0.53)]` — the stub that had just
    been stopped — and `getent hosts github.com` inside promtail returned nothing. **Nothing
    went unhealthy and no monitor fired**; promtail simply stopped resolving its Loki push
    URL. The role now restarts the running containers whenever it rewrites resolv.conf; a
    restart is enough, since Docker regenerates the container's file from the host's on start.
    **The cost:** one 2s timeout per lookup while Pi-hole is down (`attempts` counts rounds
    over the whole list, not retries per server), and no DNS cache, since resolved's is gone
    and nothing else on this host caches.
    Verify with a marked query: the Pi is not a k3s node, so it has no flannel SNAT and must
    appear in Pi-hole's client list as its LAN address, not a `10.42.x` one.
    ENFORCED by `ansible/tests/setup/test_node_resolv_order.py`, which checks both callers of the
    shared template for order, a present fallback, and the absence of `rotate`.
    **The Pi's own name never reaches that resolver.** `sudo` resolves the local hostname on
    every invocation, and the Pi's image shipped no `/etc/hosts` line for it — so the lookup
    fell through to the Pi-hole VIP above and every `sudo` printed `unable to resolve host
    daniel-pi: Name or service not known` (2026-09-27, #2724). During a cluster or Pi-hole
    outage that lookup waits on the resolver timeout first, which is exactly when an operator
    needs this host. The role now declares `127.0.1.1 {{ ansible_hostname }}` with
    `lineinfile`, bringing the Pi up to what daniel-server and daniel-box already carry.
    **cloud-init does not own that file here**, checked on the live host 2026-09-27:
    `manage_etc_hosts` is set in neither `/etc/cloud/cloud.cfg` nor `/etc/cloud/cloud.cfg.d/`,
    and cloud-init defaults it to False, which makes the `update_etc_hosts` module in
    `cloud_init_modules` a no-op — `/etc/hosts` was last written on the image build date. So
    the line survives a reboot.
    ENFORCED by `ansible/tests/setup/test_pi_resolves_its_own_hostname.py`, which fails if the
    task goes away or its regexp stops anchoring on the address.

## Autonomous-role contract (the container-recovery cron restarts what it finds dead)

The role's other two crons only read and report. This one acts: `pi-recovery-health.sh` runs
`docker start` on any watched container it finds not running, every 5 minutes, unattended.

- **Scope / exclusions:** exactly the containers this host deploys — every `containers_list`
  entry in `inventory/host_vars/daniel-pi.yml`, plus the docker-proxy role's sub-proxies
  (`docker-proxy-lifecycle` always, `docker-proxy-codeserver` under `has_code_server`). The set
  is derived from the list, never hardcoded, so a new Pi service joins it on the next deploy.
  It runs **one `docker start` per watched container per cycle** and nothing else: never
  `docker restart`, never a recreate, never a `docker rm`, never a pull, never an image or
  compose change, and nothing at all on daniel-box or daniel-server. **A container an operator
  stopped on purpose is not in the set** — the set is the deploy list rather than
  `docker ps -a --filter status=exited`, which is what keeps this from resurrecting a
  deliberate `docker stop` every 5 minutes.
- **Why it acts at all:** `restart: unless-stopped` covers a container whose PROCESS exits, not
  one whose *create* fails at the OCI runtime, which is the failure this 512 MB board actually
  produces. On 2026-08-29 autoheal died with "Timeout waiting for systemd to create scope",
  sat at `RestartCount 0`, and stayed down ~50 minutes until a human ran `docker start`.
- **Mode (explicit + reversible):** `optimize_pi_recovery_restart_enabled` arms the cron, and it
  ships `true` — the cron has restarted failed-create containers since #1910, so a default-off
  flag would have the next role run remove a live check. Setting it `false` REMOVES the cron;
  the heartbeat script stays installed, so one cycle can still be run by hand. Set it in
  `inventory/host_vars/daniel-pi.yml` rather than editing the task, so the intent survives
  another session's `--tags recovery-health` run, then apply:

  ```bash
  uv run ansible-playbook ansible/initial_setup.yml --tags recovery-health -e target=daniel-pi
  ```

  Read the result with `crontab -l -u <sys_user>` on the Pi — the `pi-recovery-health.sh` line
  is gone when disarmed. `sudo crontab -r`-style edits on the host are undone by the next role
  run; the flag is not. Detection and remediation share one script, so disarming the restart
  also ends the heartbeat: the "Daniel Pi Recovery" monitor trips its 600s watchdog within ten
  minutes of the crons stopping. That is the intended signal, not a second fault.
- **Authoritative sources:** `docker ps` against the real socket (not docker-proxy, so it still
  reports docker-proxy's own death), and `docker inspect` read BEFORE the restart — the restart
  erases the exit code, the daemon's `Error` string and `FinishedAt` of the instance that died.
- **Required evidence, every cycle:** a Kuma push to "Daniel Pi Recovery" and a line in
  `/var/log/pi-health/health.log`, which the Pi's promtail ships to Loki under `job="syslog"`.
  A cycle that intervened reads `not running: <name> [status=… exit=… error=…]; restarted:
  <name>`, and `restart FAILED: <name>` when the `docker start` did not take.
- **It pushes `down` on a cycle it fixed**, on purpose: an `up` after a successful restart would
  make a container crashing every 5 minutes read green forever. The `# DECIDED:` marker in the
  script carries the re-proposal that was declined.
- **Next-run review:** a repeat `restarted:` for the same container is not a solved problem —
  it is the thing to fix upstream. Read the week's `status=down` lines before widening the
  watch set or adding a second action.

ENFORCED by `ansible/tests/setup/test_pi_recovery_restarts_and_reports.py`, which renders the
script and runs it against a stub `docker`.

## Notable
- **Handlers live in the playbook, not this role:** `Reboot Pi`, `Restart Watchdog`,
  `Restart earlyoom`, `Restart systemd-journald` are defined in `initial_setup.yml`. The
  role only `notify:`s them. Adding a new `notify:` here requires a matching handler in
  that playbook.
- **A zram config change reboots the Pi; it is never restarted live.** The `Restart ZRAM`
  handler that used to do this swapoffs the device onto the SD swapfile, and on this box
  that takes longer than systemd's 90 s stop timeout: on 2026-09-18 the swapoff was
  killed half-way, the device stayed active at the old size, the start half failed with
  the device still in use, the unit read `failed`, and 150 MB of swap sat on the SD card
  at memory PSI `full avg60` 42% until a reboot. The task comment carries the numbers.
- GPU/watchdog/Log2Ram/zram changes `notify: Reboot Pi` — expect a reboot when they change.
- Vars are set inline in the role (`optimize_pi_gpu_memory_mb`, `optimize_pi_zram_percentage`).
