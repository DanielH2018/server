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
  `watchdog`, `debloat`, `earlyoom`. The shared prep tasks are dual-tagged (`Set variables` →
  `[gpu-mem, zram]`; the config.txt path detection → `[gpu-mem, watchdog]`) so
  tag-scoped runs still get the facts they consume. `log2ram` also covers the log
  RAM-budget tasks (journald cap, acct retention) — they exist because of the tmpfs.

## What it does (`tasks/main.yml`)
1. **Config path detection** — picks `/boot/firmware/config.txt` (Bookworm) vs
   `/boot/config.txt` (Bullseye).
2. **GPU memory split** — `gpu_mem=16` to reclaim RAM (headless).
3. **ZRAM** — installs `zram-tools`; `PERCENT=75` + `ALGO=zstd` (75% is uncompressed
   *capacity* ~343 MB; zstd's ~3.5:1 ratio makes that cost ≈ what lz4 paid for 50%).
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
   not LVFS.
7. **Log RAM budget** — `/var/log` is log2ram's 128 MB RAM-backed tmpfs (was 81% full
   2026-06-11): a Pi journald drop-in (`60-homelab-pi.conf`, `SystemMaxUse=32M`) overrides
   initial_setup's server-sized 1G cap, and `ACCT_LOGGING="3"` cuts pacct retention from
   30 daily generations (savelog via `/etc/cron.daily/acct`, ~28 MB/day of healthcheck
   exec churn) to 3.
8. **earlyoom** — kills the largest process when BOTH avail mem AND free swap drop
   under 10%, with `--avoid` shielding systemd/sshd/dockerd/containerd/watchdog.
   Before this, the only escape from a memory spiral was the hardware watchdog
   hard-rebooting at load 24 after ≥10 min of stall.

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
