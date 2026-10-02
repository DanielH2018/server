# optimize_pi — Raspberry Pi host tuning

Low-level OS, hardware and resolver tuning for the Pi. **Not a container role** — this is a
host-setup role under `ansible/roles/setup/`, run by `initial_setup.yml`, not `deploy.yml`.
See repo-root `CLAUDE.md` for conventions. The measurements behind every number here, and the
incident each step was added after, are in `docs/pi-host-tuning-record.md`.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's tasks, timer templates, defaults or playbook entry, or a schedule var in group_vars/all.yml. -->
- **Applied by:** `initial_setup.yml --tags "optimize_pi"` when `inventory_hostname ==
  optimize_pi_host`
- **Crons (3):**
  - `Pi SD-card health heartbeat` — `*/5 * * * *`
  - `Pi rotated-log integrity sweep` — `40 1 * * *`
  - `Pi container-recovery heartbeat` — `*/5 * * * *`
<!-- /generated_from -->

## Where it runs
- Invoked from `ansible/initial_setup.yml` under `when: inventory_hostname == optimize_pi_host` (`daniel-pi`, set in `group_vars/all.yml`) —
  **Pi only**. Run it with
  `uv run ansible-playbook ansible/initial_setup.yml --tags "optimize_pi" -e target=daniel-pi`,
  `--check` first, because several changes trigger a reboot. **`-e target=`, not `--limit`:**
  the play's `hosts:` defaults to the local hostname, so `--limit daniel-pi` intersects to zero
  hosts and silently does nothing.
- **Granular tags** (one section without the whole role): `gpu-mem`, `zram`, `log2ram`,
  `watchdog`, `debloat`, `earlyoom`, `apt-timers`, `node-exporter-host`, `sd-health`,
  `recovery-health`, `gz-integrity`, `pi-dns`. **A task a scoped run needs carries every
  consuming tag**, or a scoped run installs half a check — the four live cases are in
  `docs/pi-host-tuning-record.md`.
- **The two health crons source `/usr/local/lib/kuma-push-lib.sh`**, which `initial_setup`
  installs under `tags: [always]`. Both guard the `source` with `|| exit 1`, so a host missing
  the lib drops its heartbeat loudly instead of reading green.

## What it does (`tasks/main.yml`)

Fourteen steps, in this order: config-path detection, the `gpu_mem=16` split, zram, Log2Ram,
the hardware watchdog, debloat, the log RAM budget, earlyoom, the apt timers, node_exporter as
a host unit, the SD-card health heartbeat, the container-recovery heartbeat, the durable health
log, and the resolver. `docs/pi-host-tuning-record.md` has each step's numbers and incident —
read it before you change a step. Keep these rules:

- **Do not shrink zram's `PERCENT` to reclaim RAM.** The arithmetic runs the other way: zram is
  the cheapest RAM on the box, and what it cannot hold goes to the SD swapfile.
- **Log2Ram runs with `--sparse` patched out of both `rsync` lines, and not with
  `USE_RSYNC=false`** — that fallback has no `--delete`. The task's `# DECIDED:` marker holds
  the byte evidence.
- **Unmasking rsyslog means flipping `has_rsyslog: false`** in `host_vars/daniel-pi.yml` too,
  because initial_setup's `Restart rsyslog` handler fails on a masked unit (#1946).
- **Every log cap is a cap, never a disable**
  (`ansible/roles/setup/optimize_pi/defaults/main.yml:optimize_pi_log2ram_size` and the four
  retention caps under it) — `docs/security-tools.md` has the triage row for each consumer.
- **Retire the `node-exporter` container by hand before the first `node-exporter-host` apply.**
  It publishes the IP and port the unit binds.
- **The resolver's ORDER is the whole failover mechanism, and `rotate` stays unset.**
- **A missing, stale or short-file-count `gzip -t` verdict is itself a `down`**, and the health
  log's `date -Is` timestamp shape is what makes its line parse.

ENFORCED:
`ansible/tests/setup/test_optimize_pi_declares_log2ram_sizes.py::test_the_role_checks_the_duplicate_journald_aware_lines_agree`,
`ansible/tests/setup/test_pi_health_log_line_shape.py::test_the_timestamp_format_is_the_load_bearing_half`,
`ansible/tests/setup/test_node_resolv_order.py::test_the_live_defaults_and_template_keep_pihole_first`,
`ansible/tests/setup/test_pi_resolves_its_own_hostname.py::test_the_role_declares_the_pis_own_hostname_entry`.

## Autonomous-role contract (the container-recovery cron restarts what it finds dead)

The other two crons only read and report. This one acts: `pi-recovery-health.sh` runs
`docker start` on any watched container it finds not running, every 5 minutes, unattended.

- **Scope / exclusions:** exactly the containers this host deploys — every `containers_list`
  entry in `inventory/host_vars/daniel-pi.yml`, plus the docker-proxy role's sub-proxies. One
  `docker start` per watched container per cycle and nothing else: never a restart, recreate,
  `docker rm`, or pull, and nothing on the other two hosts.
  `docs/pi-host-tuning-record.md` has why the watch set is the deploy list rather than
  `docker ps -a --filter status=exited`.
- **Why it acts at all:** `restart: unless-stopped` covers a process exit, not a failed
  *create* at the OCI runtime — what this 512 MB board actually produces.
- **Mode (explicit + reversible):** `optimize_pi_recovery_restart_enabled` arms the cron and
  ships `true`. Setting it `false` in `inventory/host_vars/daniel-pi.yml` and re-running
  `--tags recovery-health` removes the cron and leaves the script installed.
  `docs/pi-host-tuning-record.md` has the disarm walkthrough, including why disarming doubles as
  the heartbeat's own failure signal.
- **Authoritative sources:** `docker ps` against the real socket, not docker-proxy, so it
  reports docker-proxy's own death too. `docker inspect` is read BEFORE the restart, which
  erases the exit code, the daemon's `Error` string and `FinishedAt`.
- **Required evidence, every cycle:** a Kuma push to "Daniel Pi Recovery" and a
  `/var/log/pi-health/health.log` line naming what died and whether the restart took.
- **It pushes `down` on a cycle it fixed**, on purpose — an `up` after a successful restart
  would hide a container crashing every 5 minutes. The script's `# DECIDED:` marker has the
  declined re-proposal.
- **Next-run review:** a repeat `restarted:` for the same container needs fixing upstream.
  Read the week's `status=down` lines before widening the watch set.

ENFORCED:
`ansible/tests/setup/test_pi_recovery_restarts_and_reports.py::test_a_dead_container_is_restarted`,
which renders the script and runs it against a stub `docker`.

## Notable
- **Handlers live in the playbook, not this role:** `Reboot Pi`, `Restart Watchdog`,
  `Restart earlyoom` and `Restart systemd-journald` are defined in `initial_setup.yml`; a new
  `notify:` here needs a matching handler there.
- **A zram config change reboots the Pi; the device is never restarted live.**
  `docs/pi-host-tuning-record.md` has why. GPU, watchdog and Log2Ram changes `notify: Reboot
  Pi` too, so expect a reboot when any of the four change.
