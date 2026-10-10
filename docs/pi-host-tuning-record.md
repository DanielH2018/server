# Pi host tuning record

What each step of the `optimize_pi` role does on daniel-pi, and the measurements behind the
numbers it sets. `ansible/roles/setup/optimize_pi/CLAUDE.md` carries the rules a session must
not break and indexes the steps; this page is the working-out, read when you edit one of them.

The board is a Raspberry Pi Zero 2 W with 512 MB of RAM, so nearly every number here was
picked against a memory ceiling rather than a CPU one.

## Granular tag rules

The role's tags let one section run without the whole role, and four cases make a task carry a
tag it does not obviously belong to:

- The shared prep tasks are dual-tagged: `Set variables` is `[gpu-mem, zram]`, and the
  config.txt path detection is `[gpu-mem, watchdog]`, so a scoped run still gets the facts it
  consumes.
- `log2ram` also covers the log RAM-budget tasks — the journald cap, acct retention, the auditd
  cap and its rotation cleanup, sysstat retention. They exist only because of the tmpfs.
- `sd-health` and `recovery-health` each create `/var/log/pi-health` and its logrotate stanza,
  so either tag alone leaves the cron it installs able to write its verdict.
- Everything the rotated-log sweep needs carries BOTH `sd-health` and `gz-integrity`, because
  the SD-card heartbeat reads that sweep's verdict and treats a missing one as a `down`. A
  `--tags sd-health` run therefore installs the sweep, primes its verdict and schedules it too.

## Boot config, GPU split and zram

The role picks `/boot/firmware/config.txt` (Bookworm) over `/boot/config.txt` (Bullseye)
before it writes either, and sets `gpu_mem=16` to reclaim RAM on a headless host.

zram comes from `zram-tools` with `PERCENT=100` and `ALGO=zstd`. 100% is uncompressed
*capacity* of about 456 MB, raised from 75% on 2026-09-18 when the device was 77% full and
31 MB had spilled to the SD swapfile; the task comment has the numbers.

**Do not shrink `PERCENT` to reclaim RAM** — the arithmetic runs the other way. Measured
2026-08-29 from `/sys/block/zram0/mm_stat`: 275.7 MiB stored in 62.4 MiB of physical RAM, a
**4.61:1** ratio, better than the ~3.5:1 the role first assumed, with `mem_used_max` at
76.6 MiB as the historical worst case. zram is the cheapest RAM on the box, and pages it
cannot hold go to the SD-card swapfile at roughly 1000× the latency.

Two zram-aware VM sysctls go with it. `vm.swappiness=130` says zram swap is cheaper than
evicting hot page cache — daniel-server runs 10 for the opposite reason. `vm.page-cluster=0`
turns off readahead on a random-access device, worth about 8× lower swap-in latency.

## Log2Ram, and why `--sparse` is patched out

The role adds the Azlux repo and installs `log2ram` to spare the SD card from log writes.
**Then it patches `--sparse` out of both of Log2Ram's `rsync` command lines**, because
`--sparse` with `--inplace` corrupts any file whose content ends in NUL bytes. rsync seeks
over the source's NUL run rather than writing it, and `--inplace` writes into the live
destination, so the previous file's bytes survive at those offsets. That clobbered the
trailing NULs of the gzip ISIZE field in 6 of 9 rotated `/var/log/apt/history.log.*.gz` on
daniel-pi, which `gzip -t` reports as "invalid compressed data--length error" (#2694).

**The log content was never lost.** The CRC32 matched in every case, so `zcat` recovers every
byte, and only `gzip -t` and `zgrep`'s exit status disagree. Which files survive is a coin
flip on what the previous file left at those offsets, so a clean `gzip -t` proves nothing
about the sync.

**Do not "simplify" this to `USE_RSYNC=false`.** That conf knob takes Log2Ram's
`cp -rfup --sparse=always` fallback, which is byte-correct but has no `--delete`: files
deleted from the tmpfs would linger on the SD card, and `sync_from_disk` would restore them
into the RAM-backed `/var/log` at boot. The task carries a `# DECIDED:` marker with the byte
evidence and the rsync version it was reproduced under.

**The patch is forward-only.** Repairing the six already-corrupt files needs a privileged
write on the Pi; they are readable as they stand and age out as they rotate.

## Watchdog and debloat

The hardware watchdog is `dtparam=watchdog=on` plus the `watchdog` daemon, which reboots the
board when the 1-minute load passes 24.

The debloat step purges Open vSwitch, which was installed with no bridges and no netplan
config yet mlockall-pinned about 14 MB, and snapd, which had zero snaps. Both are
dependency-safe: netplan only Suggests OVS. It also purges fwupd — its hourly
`fwupd-refresh.timer` swap-thrashed the 512 MB board into healthcheck-timeout storms and
autoheal restart loops while never surviving its own 25 s dbus activation timeout, and Pi
firmware arrives through apt rather than LVFS.

Three things are masked rather than purged, so a package update cannot silently re-enable
them:

- **rsyslog**, whose text logs were a second copy of the journal written into the RAM-backed
  `/var/log`. journald is the log of record here, and fail2ban reads the journal. The mask is
  why `host_vars/daniel-pi.yml` sets `has_rsyslog: false`: initial_setup's rsyslog filter
  block reads that flag and skips, because its `Restart rsyslog` handler fails on a masked
  unit (#1946). Unmasking rsyslog means flipping that flag too.
- **wpa_supplicant**, because the Pi is on wired ethernet with `wlan0` DOWN.
- The stale `aideinit` log at `/var/log/aide/aide.log`, about 9 MB of dead RAM — the weekly
  `aide --check` logs to journald.

Together these reclaimed about 17 MB on 2026-07-06.

## The log RAM budget

`/var/log` is log2ram's RAM-backed tmpfs, and the role declares its size rather than
inheriting it, with `LOG_DISK_SIZE`, `ZL2R` and `COMP_ALG` beside it:

--8<-- "assets/generated/fragments/pi-log2ram.md"

log2ram 1.7.2 ships exactly those four values and daniel-pi's `/etc/log2ram.conf` is
byte-identical to upstream's, so the declaration writes nothing today. It costs something only
when a package default moves, which is when the caps below need the number pinned — log2ram
shipped `SIZE=40M` before 128M. #2714 read the live file as hand-edited; it is not, and its
duplicate `JOURNALD_AWARE=true` is upstream's own bug at 1.7.2, fixed on master.

**The duplicate line stays.** Both copies read `true`, log2ram sources the file, so the last
assignment wins and the duplicate is inert — while deleting a shipped line would make the
conffile dpkg-MODIFIED and manufacture the apt-upgrade conffile prompt #2714 wanted gone. What
the role checks instead is that the copies AGREE (#2726): a task counts the DISTINCT
`JOURNALD_AWARE=` values and fails on anything but one, so two lines that disagree — where the
effective value is silently whichever sits last — stop the play, and a later log2ram that ships
the line once passes unchanged.

The caps themselves, set when the tmpfs was 81% full on 2026-06-11:

- A Pi journald drop-in, `60-homelab-pi.conf` with `SystemMaxUse=32M`, overrides
  initial_setup's server-sized 1G cap.
- `ACCT_LOGGING="3"` cuts pacct retention from 30 daily generations (savelog via
  `/etc/cron.daily/acct`, about 28 MB/day of healthcheck exec churn) to 3.
- auditd is capped to a 12 MB ceiling.
- `HISTORY=2` cuts sysstat's shipped 7 days — 18 sa/sar files, 9.9 MB — to 2.

**Every cap here is a cap, never a disable.** `sar`, `lastcomm` and `ausearch` each have a
triage row in `docs/security-tools.md`, so a census that finds no machine consumer has not
found that nothing reads them.

**These trims pay twice.** `Shmem` is about 2.6 MB against 75 MB of content in the tmpfs, so
most of `/var/log` is itself swapped into zram. Freeing a megabyte here frees both the
compressed physical page *and* a megabyte of zram capacity — and capacity is the axis under
pressure, since zram runs about 81% full with the SD-card swapfile already carrying overflow.

## earlyoom, and the guard that was inert for its whole life

earlyoom kills the largest process when available memory drops under 10%, with `--avoid`
shielding systemd, sshd, dbus-daemon, dockerd, containerd and the watchdog. Before it, the
only escape from a memory spiral was the hardware watchdog hard-rebooting at load 24 after at
least ten minutes of stall.

**The swap threshold is deliberately non-binding (`-s 100,100`), and that reverses an earlier
setting.** It read `-s 10`, on the theory that a true spiral meant memory AND swap both
exhausted.
earlyoom's own help states the rule — "both memory and swap must be below minimum for earlyoom
to act" — and with 1.37 GB of combined swap (456 MB of zram, 350 MB when this was measured, at
priority 100, plus a 1 GB SD-card swapfile at -2) the swap half was unreachable. Measured
2026-08-29 across four hours of earlyoom's own report lines: memory available never left
30-34%, free swap never left 77-79%. **The guard was inert for its entire life**, which is why
the 2026-08-29 autoheal create-failure hit a box with no escape hatch at all.

`dbus-daemon` is in `--avoid` because Docker's cgroup driver is `systemd`: runc asks systemd
over dbus for a scope on every container start, so killing it leaves nothing able to start a
container.

## apt timers pinned to the quiet hour

A drop-in per timer (`apt-timers`, `optimize_pi_apt_timers`) pins each timer to one daily time,
±10 min:

--8<-- "assets/generated/fragments/pi-apt-timers.md"

Ubuntu's default fires the
update half twice a day at any hour (`6,18:00` plus 12 h of randomness), and on this box each
firing is a four-minute 60 MB burst: measured 2026-09-18 at 15:05Z, memory PSI `full avg10`
45%, load 9.9, an RCU stall (#2007). The drop-in clears the packaged schedule with an empty
`OnCalendar=` first, and `ansible/tests/setup/test_pi_apt_timers_pinned.py` requires that line
and the window. The Pi memory budget review of 2026-09-18 has the host's numbers.

## node_exporter as a host unit

`node-exporter-host` and the `optimize_pi_node_exporter_*` vars install the upstream v1.12.1
arm64 tarball, checksum-pinned, under `/opt`, with `/usr/local/bin/node_exporter` a symlink to
the pinned version — a bump moves the link, which restarts `node_exporter.service`. It runs as
the `node_exporter` system user on the LAN IP's port 9100 with the collector flags the
container ran, plus a UFW allow from `lan_subnet`.

It replaced the `node-exporter` container on 2026-09-18 (#2005; the retired role is at
`git show 2460d0675fd748e70fcbcde87185371ffd62402b:ansible/roles/containers/archive/node-exporter/`).
The trade was one containerd shim, about 5 MB, off a host that keeps 10-25 MB free — and this
was the one container whose host form changes no decision.
`ansible/tests/setup/test_pi_node_exporter_host_unit.py` holds the unit to the archived
compose's collector set and the version to the cluster DaemonSet's.

**HISTORY — the node_exporter cutover.** It retired the container by hand (`docker rm -f node-exporter` and its `containers/node-exporter/` directory) before the first apply, because the container published the same IP and port the unit binds.

## SD-card health heartbeat, and the rotated-log sweep it carries

SD cards have no SMART, so `templates/pi-sd-health.sh.j2` (cron, every 5 minutes) pushes the
root filesystem's ext4 `errors_count` to the static "Daniel Pi SD Health" Kuma push monitor
through the LAN-only Authelia bypass on `^/api/push/`. A nonzero count is an explicit `down`,
and a dead cron or host trips the 600s push watchdog. The token is `pi_sd_health_push_token`
in `secrets.yml`.

**The same push carries the rotated-log integrity verdict** (#2715).
`templates/pi-gz-integrity.sh.j2` (cron, daily 01:40 UTC) runs `gzip -t` over every rotated
`.gz` under `/var/log` and `/var/hdd.log` and writes a two-line verdict to
`optimize_pi_gz_integrity_state_file`. The heartbeat reads that file and pushes `down` on a
failing verdict, on a verdict older than `optimize_pi_gz_integrity_max_age_h`, and on no
verdict at all. Three things about it:

- **`/var/hdd.log` is the load-bearing tree.** `/var/log` is the RAM tmpfs, rewritten from RAM
  on every rotation; `/var/hdd.log` is the copy on the card and the destination of the sync
  that corrupted 37 files in #2694. Running the sweep against the live Pi on 2026-09-27 found
  37 of 144 failing, every one of them under `/var/hdd.log` and none under `/var/log`. A
  `/var/log`-only sweep read green through all of #2694.
- **It is daily, not every 5 minutes.** 1.7s wall (1.44s user) over 144 files and 9.7 MB warm,
  about 13s with the SD-side copies cold, measured by running the script on the Pi. At the
  heartbeat's cadence that would be about 8 min/day of CPU and 2.8 GB/day of reads.
- **A file count below `optimize_pi_gz_integrity_min_files` is itself a `down`.** `gzip -t`
  over no files exits 0, so without the floor a moved root or a logrotate change turns the
  check green forever.

The verdict lives under `/var/lib`, not `/var/log`: a verdict in the tmpfs is gone after a
reboot, and the heartbeat would then report a missing verdict for up to a day.

## Container-recovery heartbeat

AutoKuma reconciles monitors from the `uptime-kuma` role's rendered declarations and has no
Docker source, so the Pi's containers have no liveness monitor of their own. The two that die silently are `autoheal`, which restarts unhealthy
containers, and `docker-proxy`, the read-only socket Alloy's container-log discovery reads: a
dead autoheal stops recovering Pi containers, and a dead docker-proxy stops this host's
container logs reaching Loki while Alloy keeps running with zero targets.

`templates/pi-recovery-health.sh.j2` (cron, every 5 minutes) watches **every container the
host deploys** — each `containers_list` entry, plus `docker-proxy-lifecycle` and, under
`has_code_server`, `docker-proxy-codeserver`, which are services inside the docker-proxy
role's compose file rather than list entries. It reads `docker ps` against the real socket
through the docker group, so it still reports docker-proxy's own death, and pushes to the
static "Daniel Pi Recovery" Kuma push monitor over the same LAN-only `^/api/push/` bypass. Any
one container down is an explicit `down`; a dead cron or host trips the 600s watchdog. The
token is `pi_recovery_push_token` in `secrets.yml`. The set was autoheal and docker-proxy alone
until #1910: the same failed start reaches wg-easy and docker-proxy-lifecycle, and Kuma
sees neither from outside.

**It also restarts what it finds dead**, and still pushes `down` for that cycle.
`restart: unless-stopped` covers a container whose process exits, not one whose *create* fails
at the OCI runtime — the failure this box actually produces under memory pressure. On
2026-08-29 autoheal died with `Timeout waiting for systemd to create scope`, sat at
`RestartCount 0`, and stayed down about 50 minutes until a human ran `docker start`. Detection
was never the gap. Reporting `down` on a cycle that had to intervene is what keeps an
auto-restart loop visible: pushing `up` after a successful restart would make a container
crashing every 5 minutes read green forever. The script's `# DECIDED:` marker carries the
re-proposal that was declined.

**The DOWN line names why**, from `docker inspect` read BEFORE the `docker start`:
`not running: autoheal [status=exited exit=137 finished=<ts> error=<daemon text>]`. The restart
(or the next deploy's recreate) erases that state, the journal here rotates in about a day
under `SystemMaxUse=32M`, and `/var/log/pi-health/health.log` rotates daily on log2ram — so
the cron's own line, shipped to Loki under `job="syslog"`, is the only record of a Pi container
failure that survives a week (#1912). `State.Error` is flattened to one line and capped at 300
characters so it cannot split the record the alert parser reads.

**When dockerd itself is gone** — `docker version` does not answer, or every watched container
read `inspect failed` — the reason is the daemon's, so the line carries
`; dockerd: <newest non-info journalctl -u docker lines>`: systemd's `Main process exited` and
`Scheduled restart job` lines, plus dockerd's own `level=error`, capped at 400 characters
(#1922).
Not `journalctl -p warning`: dockerd's stderr lands at journal priority 6 whatever its
`level=` says, so a priority filter returns nothing. This is the only path a daemon-level
failure reaches Loki by — the Pi ships no journal, and `roles/containers/alloy/CLAUDE.md`
records the RSS measurement behind that.

### Why the watch set is the deploy list

The set is derived from `containers_list`, never hardcoded, so a new Pi service joins it on the
next deploy. It is the deploy list rather than `docker ps -a --filter status=exited`, which is
what keeps the cron from resurrecting a deliberate `docker stop` every 5 minutes. One
`docker start` per watched container per cycle is the whole action: never `docker restart`,
never a recreate, never a `docker rm`, never a pull, and nothing at all on daniel-box or
daniel-server.

### Disarming it

`optimize_pi_recovery_restart_enabled` arms the cron and ships `true` — the cron has restarted
failed-create containers since #1910, so a default-off flag would have the next role run remove
a live check. Setting it `false` REMOVES the cron; the heartbeat script stays installed, so one
cycle can still be run by hand. Set it in `inventory/host_vars/daniel-pi.yml` rather than
editing the task, so the intent survives another session's `--tags recovery-health` run, then
apply:

```bash
uv run ansible-playbook ansible/initial_setup.yml --tags recovery-health -e target=daniel-pi
```

Read the result with `crontab -l -u <sys_user>` on the Pi — the `pi-recovery-health.sh` line is
gone when disarmed. A `sudo crontab -r`-style edit on the host is undone by the next role run;
the flag is not. Detection and remediation share one script, so disarming the restart also ends
the heartbeat: the "Daniel Pi Recovery" monitor trips its 600s watchdog within ten minutes of
the crons stopping. That is the intended signal, not a second fault.

## The durable health log

Both health crons leave a record at `/var/log/pi-health/health.log`, which the Pi's Alloy
tails as its `pi_health` source under `job="syslog"`. Kuma keeps only current state, so without
this a DOWN that clears is gone — and `probe.py alerts` reconstructs episodes from
`{job="syslog"} |= "status=down"`.

Two independent gaps kept daniel-pi out of that view, and closing either alone would have
changed nothing: the crons emitted no `status=` token, because `kuma-push-lib.sh` calls
`logger` only when the *push* fails, and there was no path to Loki for it anyway, because
rsyslog is masked on this host. A file plus a static log source needs no new daemon, and
carries about 576 lines/day where the whole journal would be about 38k.

**The timestamp format is load-bearing**: `_SYSLOG_LINE_RE` wants exactly two whitespace-free
tokens before the tag, so the scripts emit `date -Is`, which is one token. Traditional syslog
format is four tokens and parses as nothing.

Adding this stream meant excluding it from monitor-bridge's Loki file-tail arm
(`machine!="daniel-pi"` in `LOKI_STREAM`) — a Pi stream under `job="syslog"` would otherwise
stop a total cluster file-tail outage from ever reaching that arm's zero threshold.

**Two deploys activate it**, because the Pi is manual-deploy: install the cron with
`initial_setup.yml --tags recovery-health -e target=daniel-pi`, which also copies the shared
push lib, then redeploy `uptime-kuma` on daniel-box so AutoKuma provisions the monitor. Do both
close together, or the fresh push monitor false-DOWNs until the first heartbeat lands.

## Resolver

A static `/etc/resolv.conf`, rendered from the SHARED
`roles/setup/common/templates/resolv.conf.j2` that daniel-box also uses, lists the cluster
Pi-hole first and a public resolver behind it, and systemd-resolved is disabled. Before this
the DHCP lease's ISP resolvers answered everything, including every internal
`*.local.<domain>` name — those resolve publicly via the Cloudflare wildcard, so the private
hostnames were leaving the LAN.

**The order is the entire mechanism.** `resolv.conf(5)` queries nameservers in the order
listed, and the `rotate` option — deliberately unset — is the one that would stop a client
always trying the first listed server first. So the Pi falls through to the public resolver while
Pi-hole is unreachable and returns to it on the very next query, automatically, in both
directions, with no daemon watching anything.

**HISTORY — the resolved drop-in.** This replaced PR #693's resolved drop-in, which did not work. systemd-resolved treats its `DNS=` list as interchangeable peers, so `Current DNS Server` flipped between 1.1.1.1 and 10.0.0.243 unprompted within minutes of that deploy. Disabling resolved also removes the LINK scope a drop-in could not reach.

**Running containers keep the old resolver until they restart.** Docker's embedded resolver pins
its upstreams at container START, so a changed host resolv.conf leaves every running container
forwarding to whatever was there before. Nothing goes unhealthy and no monitor fires: on the
2026-09-01 cutover the Pi's log shipper stopped resolving its Loki push URL and nothing else
changed. The role restarts the running containers whenever it rewrites resolv.conf, and a restart
is enough, since Docker regenerates the container's file from the host's on start.

**The cost:** one 2s timeout per lookup while Pi-hole is down, because `attempts` counts rounds
over the whole list rather than retries per server, and no DNS cache, since resolved's is gone
and nothing else on this host caches. Verify with a marked query: the Pi is not a k3s node, so
it has no flannel SNAT and must appear in Pi-hole's client list as its LAN address rather than
a `10.42.x` one.

**The Pi's own name never reaches that resolver.** `sudo` resolves the local hostname on every
invocation, and the Pi's image shipped no `/etc/hosts` line for it — so the lookup fell through
to the Pi-hole VIP above and every `sudo` printed `unable to resolve host daniel-pi: Name or
service not known` (2026-09-27, #2724). During a cluster or Pi-hole outage that lookup waits on
the resolver timeout first, which is exactly when an operator needs this host. The role now
declares `127.0.1.1 {{ ansible_hostname }}` with `lineinfile`, bringing the Pi up to what
daniel-server and daniel-box already carry.

**cloud-init does not own that file here**, checked on the live host 2026-09-27:
`manage_etc_hosts` is set in neither `/etc/cloud/cloud.cfg` nor `/etc/cloud/cloud.cfg.d/`, and
cloud-init defaults it to False, which makes the `update_etc_hosts` module in
`cloud_init_modules` a no-op — `/etc/hosts` was last written on the image build date. So the
line survives a reboot.

## Why a zram change reboots the Pi

The `Restart ZRAM` handler that used to restart the device live swapoffs it onto the SD
swapfile, and on this box that takes longer than systemd's 90 s stop timeout. On 2026-09-18 the
swapoff was killed half-way, the device stayed active at the old size, the start half failed
with the device still in use, the unit read `failed`, and 150 MB of swap sat on the SD card at
memory PSI `full avg60` 42% until a reboot. The task comment carries the numbers.

## See also

- `ansible/roles/setup/optimize_pi/CLAUDE.md` — the rules, the granular tags and the
  container-recovery contract.
- [Security tools](security-tools.md) — the triage rows for `sar`, `lastcomm` and `ausearch`,
  whose retention the log RAM budget caps.
