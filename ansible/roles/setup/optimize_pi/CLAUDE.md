# optimize_pi — Raspberry Pi host tuning

Low-level OS, hardware and resolver tuning for the Pi. **Not a container role** — this is a
host-setup role under `ansible/roles/setup/`, run by `initial_setup.yml`, not `deploy.yml`.
See repo-root `CLAUDE.md` for conventions. The measurements behind every number here, and the
incident each step was added after, are in `docs/pi-host-tuning-record.md`.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's tasks, timer templates, defaults or playbook entry, or a schedule var in group_vars/all.yml. -->
- **Applied by:** `initial_setup.yml --tags "optimize_pi"` when `inventory_hostname ==
  optimize_pi_host`
- **Crons / timers:** none (no `ansible.builtin.cron` task in `tasks/`, no
  `templates/*.timer.j2`)
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
  `recovery-health`, `pi-dns`. **A task a scoped run needs carries every
  consuming tag**, or a scoped run installs half a check — the three live cases are in
  `docs/pi-host-tuning-record.md`.
- **The Pi's three heartbeats are not this role's since H3.8b.** The `homelab-daniel-pi`
  package ships them as systemd timers from `hosts/components/pi-heartbeats/`, whose
  `component.yml` carries their defaults and the recovery heartbeat's Autonomous-role contract.
  The `sd-health` and `recovery-health` tags only stage the push-token files those timers read.

## What it does (`tasks/main.yml`)

Twelve steps, in this order: config-path detection, the `gpu_mem=16` split, zram, Log2Ram,
the hardware watchdog, debloat, the log RAM budget, earlyoom, the apt timers, node_exporter as
a host unit, the interim push-token files the packaged heartbeats read, and the resolver. `docs/pi-host-tuning-record.md` has each step's numbers and incident —
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
- **The heartbeats' two load-bearing rules moved with them** to
  `hosts/components/pi-heartbeats/`: a missing, stale or short-file-count `gzip -t` verdict is
  itself a `down`, and the health log's `date -Is` timestamp shape is what makes its line parse.
- **`/etc/homelab/interim/pi_sd_health_push_token` and `pi_recovery_push_token` are
  Ansible-owned and interim:** the H3.8b package only reads them through `LoadCredential=`,
  and H4's last PR retires them.

ENFORCED:
`ansible/tests/setup/test_optimize_pi_declares_log2ram_sizes.py::test_the_role_checks_the_duplicate_journald_aware_lines_agree`,
`ansible/tests/setup/test_pi_health_log_line_shape.py::test_the_timestamp_format_is_the_load_bearing_half`,
`ansible/tests/setup/test_node_resolv_order.py::test_the_live_defaults_and_template_keep_pihole_first`,
`ansible/tests/setup/test_pi_resolves_its_own_hostname.py::test_the_role_declares_the_pis_own_hostname_entry`.

## Notable
- **Two of this role's handlers live in [[initial_setup]]:** `Restart Watchdog` and
  `Restart systemd-journald`. Each belongs to an ordered pair defined there, and a handler
  defined here would inherit this role's host gate. `Reboot Pi` and `Restart earlyoom` are in
  this role's `handlers/main.yml`.
- **A zram config change reboots the Pi; the device is never restarted live.**
  `docs/pi-host-tuning-record.md` has why. GPU, watchdog and Log2Ram changes `notify: Reboot
  Pi` too, so expect a reboot when any of the four change.
