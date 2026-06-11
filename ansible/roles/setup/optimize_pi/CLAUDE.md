# optimize_pi — Raspberry Pi hardware tuning

Low-level OS/hardware tuning for the Pi. **Not a container role** — this is a host-setup
role under `ansible/roles/setup/`, run by `initial_setup.yml`, not `deploy.yml`.
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
  `watchdog`, `debloat`. The shared prep tasks are dual-tagged (`Set variables` →
  `[gpu-mem, zram]`; the config.txt path detection → `[gpu-mem, watchdog]`) so
  tag-scoped runs still get the facts they consume.

## What it does (`tasks/main.yml`)
1. **Config path detection** — picks `/boot/firmware/config.txt` (Bookworm) vs
   `/boot/config.txt` (Bullseye).
2. **GPU memory split** — `gpu_mem=16` to reclaim RAM (headless).
3. **ZRAM** — installs `zram-tools`, `PERCENT=50` compressed swap.
4. **Log2Ram** — adds the Azlux repo + installs `log2ram` to spare the SD card from log writes.
5. **Hardware watchdog** — `dtparam=watchdog=on` + `watchdog` daemon, auto-reboot if 1-min
   load > 24.
6. **Debloat** — purges Open vSwitch (was installed but had no bridges/netplan config,
   yet mlockall-pinned ~14 MB) and snapd (zero snaps installed). Verified dependency-safe;
   netplan only Suggests OVS. Also purges fwupd: its hourly `fwupd-refresh.timer`
   swap-thrashed the 512 MB board (healthcheck-timeout storms → autoheal restart loops)
   while never surviving its own 25 s dbus activation timeout; Pi firmware comes via apt,
   not LVFS.

## Notable
- **Handlers live in the playbook, not this role:** `Reboot Pi`, `Restart ZRAM`,
  `Restart Watchdog` are defined in `initial_setup.yml`. The role only `notify:`s them.
  Adding a new `notify:` here requires a matching handler in that playbook.
- GPU/watchdog/Log2Ram changes `notify: Reboot Pi` — expect a reboot when they change.
- Vars are set inline in the role (`optimize_pi_gpu_memory_mb`, `optimize_pi_zram_percentage`).
