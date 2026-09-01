# optimize_pi — Raspberry Pi host tuning

Low-level OS, hardware and resolver tuning for the Pi. **Not a container role** — this is a
host-setup role under `ansible/roles/setup/`, run by `initial_setup.yml`, not `deploy.yml`.
See repo-root `CLAUDE.md` for conventions.

## Where it runs
- Invoked from `ansible/initial_setup.yml`:
  `{ role: optimize_pi, tags: ["optimize_pi"], when: inventory_hostname == 'daniel-pi' }`
  — **Pi only** (guarded by `inventory_hostname`).
- Run it with: `uv run ansible-playbook ansible/initial_setup.yml --tags "optimize_pi" -e target=daniel-pi`
  (use `--check` first — several changes trigger a reboot). NB `-e target=`, not `--limit`:
  the play's `hosts:` defaults to the local hostname, so `--limit daniel-pi` from the
  server intersects to zero hosts and silently does nothing.
- **Granular tags** (one section without the whole role): `gpu-mem`, `zram`, `log2ram`,
  `watchdog`, `debloat`, `earlyoom`, `sd-health`, `recovery-health`, `pi-dns`. The shared prep
  tasks are dual-tagged (`Set variables` →
  `[gpu-mem, zram]`; the config.txt path detection → `[gpu-mem, watchdog]`) so
  tag-scoped runs still get the facts they consume. `log2ram` also covers the log
  RAM-budget tasks (journald cap, acct retention, the auditd cap and its rotation
  cleanup, sysstat retention) — they exist because of the tmpfs. `sd-health` and
  `recovery-health` each also create `/var/log/pi-health` and its logrotate stanza, so
  either tag alone leaves the cron it installs able to write its verdict.
- **The two health crons source `/usr/local/lib/kuma-push-lib.sh`**, installed by the
  `initial_setup` role under `tags: [always]` so a `--tags sd-health` or `--tags recovery-health`
  run still copies it. Both scripts guard the `source` with `|| exit 1`, so a host missing the
  lib drops its heartbeat loudly instead of reporting green.

## What it does (`tasks/main.yml`)
1. **Config path detection** — picks `/boot/firmware/config.txt` (Bookworm) vs
   `/boot/config.txt` (Bullseye).
2. **GPU memory split** — `gpu_mem=16` to reclaim RAM (headless).
3. **ZRAM** — installs `zram-tools`; `PERCENT=75` + `ALGO=zstd` (75% is uncompressed
   *capacity* ~343 MB; zstd's compression makes that cost ≈ what lz4 paid for 50%).
   **Do not shrink `PERCENT` to reclaim RAM** — the arithmetic runs the other way. Measured
   2026-08-29 from `/sys/block/zram0/mm_stat`: 275.7 MiB stored in 62.4 MiB of physical RAM,
   a **4.61:1** ratio (better than the ~3.5:1 this line assumed), with `mem_used_max` 76.6 MiB
   as the historical worst case. It is the cheapest RAM on the box, and pages it cannot hold
   go to the SD-card swapfile at roughly 1000× the latency.
   Plus zram-aware VM sysctls: `vm.swappiness=130` (zram swap is cheaper than evicting
   hot page cache — the server runs 10 for the opposite reason) and `vm.page-cluster=0`
   (no readahead on random-access zram; ~8× lower swap-in latency).
4. **Log2Ram** — adds the Azlux repo + installs `log2ram` to spare the SD card from log writes.
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
7. **Log RAM budget** — `/var/log` is log2ram's 128 MB RAM-backed tmpfs (was 81% full
   2026-06-11): a Pi journald drop-in (`60-homelab-pi.conf`, `SystemMaxUse=32M`) overrides
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
   minimum for earlyoom to act" — and with 1.37 GB of combined swap (350 MB zram at
   priority 100, a 1 GB SD-card swapfile at -2) the swap half was unreachable. Measured
   2026-08-29 across four hours of earlyoom's own report lines: mem avail never left 30-34%,
   free swap never left 77-79%. **The guard was inert for its entire life**, which is why the
   2026-08-29 autoheal create-failure hit a box with no escape hatch at all. `dbus-daemon` is
   in `--avoid` because Docker's cgroup driver is `systemd`: runc asks systemd over dbus for a
   scope on every container start, so killing it leaves nothing able to start a container.
9. **SD-card health heartbeat** — SD cards have no SMART, so `templates/pi-sd-health.sh.j2`
   (cron, */5) pushes the root fs's ext4 `errors_count` to the static "Daniel Pi SD
   Health" Kuma push monitor (uptime-kuma role) via the LAN-only Authelia bypass on
   `^/api/push/` (authelia role). Nonzero count = explicit `down`; a dead cron/host
   trips the 600s push watchdog. Token: `pi_sd_health_push_token` in `secrets.yml`.
10. **Container-recovery heartbeat** — AutoKuma reads only the SERVER's docker socket, so the
    Pi's `autoheal` (restarts unhealthy containers) and `docker-proxy` (the read-only socket
    promtail's container-log discovery and glances both read) have no liveness monitor — a dead
    autoheal silently stops recovering Pi containers, and a dead docker-proxy stops this host's
    container logs reaching Loki while promtail keeps running with zero targets and glances
    keeps answering its own HTTP, so nothing else goes red.
    `templates/pi-recovery-health.sh.j2` (cron, */5) pushes both containers' running-state
    (via `docker ps`, which reads the real socket through the docker group — so it still
    reports docker-proxy's own death) to the static "Daniel Pi Recovery" Kuma push monitor
    (uptime-kuma role), same LAN-only `^/api/push/` bypass. Either down = explicit `down`; a
    dead cron/host trips the 600s watchdog. Token: `pi_recovery_push_token` in `secrets.yml`.
    **It also RESTARTS what it finds dead**, and still pushes `down` for that cycle.
    `restart: unless-stopped` covers a container whose process exits, not one whose *create*
    fails at the OCI runtime — the failure this box actually produces under memory pressure.
    On 2026-08-29 autoheal died with "Timeout waiting for systemd to create scope", sat at
    `RestartCount 0`, and stayed down ~50 minutes until a human ran `docker start`. Detection
    was never the gap. Reporting `down` on a cycle that had to intervene is what keeps an
    auto-restart loop visible: pushing `up` after a successful restart would make a container
    crashing every 5 minutes read green forever.
    ENFORCED by `ansible/tests/test_pi_recovery_restarts_and_reports.py`, which renders and
    runs the script against a stub `docker`.

11. **Both health crons leave a durable record** at `/var/log/pi-health/health.log`, which the
    Pi's promtail tails as its `pi-health` job under `job="syslog"`. Kuma keeps only current
    state, so without this a DOWN that clears is gone — and `probe.py alerts` reconstructs
    episodes from `{job="syslog"} |= "status=down"`. Two independent gaps kept daniel-pi out
    of that view, and closing either alone would have changed nothing: the crons emitted no
    `status=` token (`kuma-push-lib.sh` calls `logger` only when the *push* fails), and there
    was no path to Loki for it anyway (rsyslog is masked here by §6, and this promtail build
    is a journal stub — verified: no `sd_journal_open`, no libsystemd, and a `journal:` dry
    run yields zero entries). A file plus a static scrape job needs no new daemon, and carries
    ~576 lines/day where the whole journal would be ~38k.
12. **Resolver** — a systemd-resolved drop-in (`50-homelab-dns.conf`) sets
    `DNS=<dns_k8s_vip> 1.1.1.1` with `Domains=~.`, so the Pi resolves through the cluster
    Pi-hole and falls back to a public resolver. Before this the DHCP lease's ISP resolvers
    answered everything, including every internal `*.local.<domain>` name — those resolve
    publicly via the Cloudflare wildcard, so the private hostnames were leaving the LAN.
    `Domains=~.` overrides the per-link lease servers without touching netplan, which is why
    this role needs no equivalent of the k3s role's `netplan-dns.yaml.j2`.
    **It also moves the Pi's containers.** Docker's embedded resolver forwards to the host
    stub (`ExtServers: [host(127.0.0.53)]` in a container's `/etc/resolv.conf`), so all seven
    follow — promtail's `loki-homelab.local.<domain>` push URL included.
    **The fallback is load-bearing, and its cutover is slower than daniel-server's.** That
    host gets 2 seconds from glibc's `timeout:2 attempts:1` in a static `/etc/resolv.conf`;
    resolved retries a server over several seconds and caches its choice. Accepted because
    the ordering is the point: the Pi's wg-easy tunnel is the documented way in when the
    cluster's own is unreachable (`roles/containers/wg-easy/CLAUDE.md`), so this host must
    prefer the cluster for DNS and never depend on it.
    Verify with a marked query: the Pi is not a k3s node, so it has no flannel SNAT and must
    appear in Pi-hole's client list as its LAN address, not a `10.42.x` one.
    **The timestamp format is load-bearing**: `_SYSLOG_LINE_RE` wants exactly two
    whitespace-free tokens before the tag, so the scripts emit `date -Is` (one token).
    Traditional syslog format is four tokens and parses as nothing.
    ENFORCED by `ansible/tests/test_pi_health_log_line_shape.py`, which feeds the scripts'
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

## Notable
- **Handlers live in the playbook, not this role:** `Reboot Pi`, `Restart ZRAM`,
  `Restart Watchdog`, `Restart earlyoom`, `Restart systemd-journald` are defined in
  `initial_setup.yml`. The role only `notify:`s them.
  Adding a new `notify:` here requires a matching handler in that playbook.
- **ZRAM restart caveat:** the `Restart ZRAM` handler swapoffs the device, faulting
  everything stored in it back into RAM/file-swap — on a loaded box this grinds for a
  few minutes (and may trip the Pi Pressure monitor once). Harmless, but prefer quiet
  hours for zram config changes.
- GPU/watchdog/Log2Ram changes `notify: Reboot Pi` — expect a reboot when they change.
- Vars are set inline in the role (`optimize_pi_gpu_memory_mb`, `optimize_pi_zram_percentage`).
