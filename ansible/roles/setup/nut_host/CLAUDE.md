# nut_host — host half of the UPS shutdown chain

The host-side pieces of the UPS stack: the udev rule that lets the `k8s/nut` pod open the
APC UPS over USB, and a **secondary `upsmon`** on each armed host that powers the host off
when the primary declares FSD. The primary `upsd`/`upsmon` is the `k8s/nut` pod, pinned to
`ups_host`; `ansible/roles/k8s/nut/CLAUDE.md` owns the chain end to end and this file covers
only what runs on the host. Repo-root `CLAUDE.md` has the conventions.

## Where it runs
- A setup-plane role: `ansible/initial_setup.yml` includes it, `deploy.yml` does not, and
  `deploy.sh --tags nut_host` exits 2 having deployed nothing. Apply it with
  `uv run ansible-playbook ansible/initial_setup.yml --tags nut_host -e target=<host>`, once
  per host.
- Gate: `when: inventory_hostname == ups_host or nut_host_secondary_armed | bool`. `ups_host`
  (daniel-server) runs it unconditionally because it owns the USB UPS. Any other host runs
  only the client half, and only after `nut_host_secondary_armed: true` is set in its
  `host_vars` — daniel-box since 2026-08-28. A secondary upsmon exists to power its host
  off, so arm a further host only after the console-attended `upsmon -c fsd` drill.
- **It lived at `ansible/roles/nut_host/` until 2026-09-17.** Outside `roles/setup/` it was
  invisible to the deployer's `_BROAD_SETUP_PREFIXES` and to `land.sh`'s plane note, so PR
  #1915 landed on master with its change unapplied and unreported on both hosts (#1916).
  `ansible.cfg`'s `roles_path` lists `ansible/roles/setup`, so the bare `role: nut_host` in
  `initial_setup.yml` resolved unchanged across the move.
  `ansible/tests/setup/test_initial_setup_roles_are_visible_to_the_deployer.py` refuses a
  sibling in that position.

## What it does (`tasks/main.yml`)
1. **udev** (`ups_host` only) — the APC USB permissions rule, then reload and trigger.
2. **Endpoint resolution** (every other host) — reads the `nut` Service's ClusterIP with
   `kubectl`, sets `nut_host_upsd_host`, and **fails the play** when `upsd` does not answer
   there. A secondary that cannot reach `upsd` would never see FSD, so the failure is loud
   on purpose.
3. **NUT client** — installs `nut-client`, renders `/etc/nut/nut.conf` (netclient mode) and
   `/etc/nut/upsmon.conf` (the `MONITOR` line against `nut_host_upsd_host`, the
   `nut_monitor_*` SOPS credentials, and the `FINALDELAY` whose comment records why the two
   hosts are not staggered), then enables `nut-monitor`.
4. **Watchdog** (`nut_host_watchdog_armed`, default true) — renders
   `/etc/nut/kuma-push.env` (the `ups_secondary_push_token`, root-only) and
   `/usr/local/bin/ups-secondary-health.sh` at `0700`, and schedules it every
   `nut_host_watchdog_interval_minutes` (10) as a `cron_file`. The script is `0700`, not
   `0755`, because it renders a credential inline;
   `test_secret_bearing_host_scripts_are_not_world_readable.py` enforces that for every path
   `secret_bearing_host_paths()` names.
5. **Render stamp** — `stamp_render.yml` records the three templates' repo hashes so
   `setup-drift-check.sh` (armed on daniel-server via `setup_drift_check_hosts`) reports a
   stale render. After a template moves or changes, that check reports drift until this
   role is re-applied on the host.

## The watchdog cron changes no state
`ups-secondary-health.sh` reads `upsmon.conf`, `systemctl is-active nut-monitor` and
`upsc ups.status`, logs a verdict, and pushes `up`/`down` to the Kuma tile
`UPS Secondary (<host>)`. It restarts, writes and deletes nothing, which is why
`test_setup_roles_have_claude_md.py` lists this role in `EXEMPT` rather than asking it for
an `## Autonomous-role contract`. The actor that does change state is `upsmon` itself, a
systemd service and not a cron, and `k8s/nut/CLAUDE.md` documents what it does on FSD.

## Verify
- `probe.py monitors` names `UPS Secondary (<host>)` per armed host; a `down` there carries
  the script's own diagnosis (`nut-monitor is inactive`, `upsd unreachable at …`).
- `ssh <host> systemctl is-active nut-monitor` and `ssh <host> upsc <endpoint> ups.status`
  are the two reads the cron makes; run them by hand when the tile disagrees with the pod.
- `ssh <host> stat -c '%a %U' /usr/local/bin/ups-secondary-health.sh` reads `700 root`.
