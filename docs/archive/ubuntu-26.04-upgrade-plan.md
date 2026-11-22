# Ubuntu 26.04 upgrade plan

All three hosts can move from Ubuntu 24.04 to 26.04.1 LTS. They move one at a time:
daniel-server, then daniel-box, with daniel-pi on its own at any time after daniel-server,
following *Upgrading a host's Ubuntu
release* in [`docs/host-packages.md`](../host-packages.md). Two PRs remain: one moves the Pi's
Docker source, and one retires 24.04. The upgrade itself is operator-only. This page records
the answers the procedure rests on, then the sequence.

## Resolved questions

Measured 2026-11-09.

1. **k3s v1.37.2 supports 26.04.** SUSE's
   [k3s v1.37 support matrix](https://www.suse.com/suse-k3s/support-matrix/all-supported-versions/k3s-v1-37/)
   lists Ubuntu 26.04 and 24.04, revised as of v1.37.1+k3s1.
1. **Longhorn v1.13.0 supports 26.04.** The
   [v1.13.0 best-practices page](https://longhorn.io/docs/1.13.0/best-practices/) lists
   Ubuntu 26.04 among the OS versions verified in release testing.
1. **Ubuntu 26.04 supports the Raspberry Pi Zero 2 W.**
   [Ubuntu's Raspberry Pi install guide](https://ubuntu.com/hardware/docs/boards/how-to/ubuntu_supported/raspberry-pi/)
   lists it for 26.04. The
   [26.04 release notes](https://documentation.ubuntu.com/release-notes/26.04/summary-for-lts-users/)
   say the Zero 2 W needs no firmware action.
1. **`do-release-upgrade` disables every apt source the host packages ship.** Its 26.04
   `rewriteDeb822Sources` marks a source `Enabled: no` when its URI is not an Ubuntu mirror,
   or when its suite does not map to the next release. That covers:
    - `homelab.sources` on every host, owned by `homelab-updater`;
    - `github-cli.sources` on daniel-server and daniel-box;
    - `docker.sources` and `azlux.sources` on daniel-pi.

   The edit breaks two things after the upgrade. `homelab-updater` reads `homelab.sources`
   as its only source list, so it sees no repository. `dpkg --verify` also reports every
   rewritten file. The fix is to copy the files aside before the upgrade, then copy them
   back before the reboot.
1. **`do-release-upgrade` would offer to remove every `homelab-*` package.** It removes the
   packages that stopped being downloadable during the run (`remove_candidates =
   now_obsolete - self.obsolete_pkgs`). Once the sources are disabled, that set includes
   every `homelab-*` package, plus `docker-ce` and `containerd.io` on daniel-pi. The text UI
   prompts `Continue [yN]`, and Enter keeps the packages. The procedure removes the prompt
   instead. `do-release-upgrade` merges `/etc/update-manager/release-upgrades.d/*.cfg`, and a
   drop-in that sets `RemoveObsoletes=False` under `[Distro]` skips the removal.
1. **The noble Docker packages run on 26.04, so the Docker change follows the Pi's upgrade.**
   Every `Depends` of the five pinned packages (`docker-ce`, `docker-ce-cli` 29.8.1,
   `containerd.io` 2.3.5, compose 5.5.1, `docker-buildx-plugin` 0.37.1) resolves against
   resolute arm64. The same versions exist in Docker's `resolute` arm64 index as `~ubuntu.26.04~resolute`.
1. **The Pi's boot partition migrates to the A/B layout, and its config survives.**
   daniel-pi has `ubuntu-server-raspi` installed. On 26.04 that package depends on
   `piboot-try`, which replaces `flash-kernel` and migrates the boot partition:
    - it deletes the `.bak` boot assets;
    - it moves the kernel, `initrd.img` and DTBs into `current/`;
    - it puts `os_prefix=current/` and `[tryboot] os_prefix=new/` at the top of
      `/boot/firmware/config.txt` and keeps every other line.

   pi-tuning's `gpu_mem=16` and `dtparam=watchdog=on` lines keep their path and survive.
   No component writes `cmdline.txt`. One set of boot assets is about 76 MB (`initrd.img` 60 MB,
   kernel 12.5 MB, DTBs and overlays 3 MB). Three sets fit in the 505 MB partition. Each
   later kernel update boots twice: once to test the new assets, then to commit them.
1. **All three hosts run cgroup v2.** `stat -fc %T /sys/fs/cgroup` prints `cgroup2fs` on each.
   26.04 refuses to upgrade a cgroup v1 host.
1. **Host scripts run under 26.04's Python 3.14.** No host script imports a stdlib module
   that Python 3.13 removed. The `host_packages (26.04, 3.14)` CI leg runs reconcile under
   3.14.
1. **sudo-rs replaces sudo.** The release-upgrade procedure in `docs/host-packages.md`
   already checks `visudo -c` from a held root shell and falls back to `sudo.ws`.
   daniel-server's upgrade records the result for
   [#7645](https://github.com/DanielH2018/server/issues/7645).
1. **The upgrade can run over SSH.** `do-release-upgrade` runs itself inside a GNU screen
   window named `ubuntu-release-upgrade-screen-window`, so a dropped session reattaches.
   Keep a console on standby: a monitor and keyboard for the two nodes, the serial UART on
   the Pi (`enable_uart=1`).
1. **The arm64 26.04 CI leg is not a gate.** `host_tools_arm64` checks only that the
   static `sops` and `chezmoi` binaries run on arm64. The procedure's gate is the
   `host_packages` matrix, which already lists 26.04.

## Scheduling rules

- **Not Saturday 23:00 to Sunday 08:15 UTC.** The weekly reboot fires Sunday 07:30 UTC, and
  `promote` defers inside the 02:05 to 08:15 window.
- **daniel-box: not near 11:32 UTC on a weekday.** That is `k3s_restart_window`.
- **One host per window.** The next host starts only when the last one passes its
  Prometheus check.

## The upgrade sequence

Each host follows the procedure. It copies the package-owned apt sources aside before the
upgrade and puts them back before the reboot. It also installs a `release-upgrades.d`
drop-in that skips obsolete removal. The notes below cover what is specific to each host.

### daniel-server

- Delete `/etc/apt/sources.list.d/docker.sources` first, from the `ubuntu` shell. No package
  owns it, and Docker left this host on 2026-08-14. `do-release-upgrade` would only disable it.

    ```bash
    sudo rm /etc/apt/sources.list.d/docker.sources
    ```
- `wezterm.list` is unowned. `do-release-upgrade` converts it to `.sources` and disables it.
  Re-enable it afterwards if WezTerm updates still matter on this host.
- The kernel already runs 7.0 (HWE), so the Intel XE graphics see no kernel jump.
- Record what sudo-rs did for [#7645](https://github.com/DanielH2018/server/issues/7645).

### daniel-box

- This host runs every Claude session, the builder, the signer and the package repository.
  Before the upgrade, stop every session and fan-out, and confirm no `promote` or builder
  run is in flight.
- The reboot takes the control plane and Pi-hole down for as long as the weekly reboot does.
- Take an etcd snapshot first. The procedure's *On daniel-box, take an etcd snapshot* step
  holds the command and its confirmation. The datastore is embedded etcd (`cluster-init: true`
  in `hosts/components/k3s-server/component.yml`), and k3s already snapshots it at 00:00 and
  12:00 UTC, locally and to R2. The on-demand snapshot moves the recovery point to the minute
  before the upgrade. [`docs/k3s-etcd-restore.md`](../k3s-etcd-restore.md) is the restore
  runbook. It says no drill exercises the agent rejoin or the Longhorn reattach after a real
  restore.

#### The daniel-box kernel moves from 6.8 to 7.0

daniel-box boots 6.8.0-149 and moves to the release's 7.0 kernel. daniel-server has run the
24.04 HWE 7.0 kernel since at least 2026-10-02, and it booted 7.0.0-43 on 2026-11-20. That is the noble
build, not the `resolute` one, so read daniel-server's `uname -r` after its own upgrade for the exact
version.

Measured on both hosts on 2026-11-21:

- Neither host has `dkms` installed, so no out-of-tree module needs a rebuild.
- Nothing under `hosts/` or `inventory/` pins a kernel version or package.
- Both hosts load the modules Longhorn uses, `iscsi_tcp` and `dm_multipath`, and Longhorn
  reports the NFS client installed on both. daniel-server's 7.0 `linux-modules` package ships
  `iscsi_tcp`, `nfs` and `nfsv4`, and daniel-server runs its Longhorn iSCSI sessions on that
  kernel. The one `ReadWriteMany`
  claim, `media-data`, is a local PV on `/srv/media`, not a Longhorn share.

daniel-server's time on 7.0 covers some of daniel-box's hardware and not the rest:

- **Covered.** The node address rides `eno1`, an Intel I226-V on the `igc` driver.
  daniel-server's link also uses `igc`. Both root disks use the `nvme` driver, from different
  vendors.
- **Not covered.** The Wi-Fi fallback link `wlp4s0` (`k3s_node_wifi_link`) is a MediaTek MT7922
  on `mt7921e`, where daniel-server has Intel `iwlwifi`. The graphics are AMD Phoenix on
  `amdgpu`, where daniel-server has Intel `xe` and `i915`. No workload uses daniel-box's GPU.

Before the reboot, `ls /boot/vmlinuz-6.8*` shows whether a 6.8 kernel stays installed as a
GRUB fallback entry.

After the reboot, check the kernel, then the node. Each check, and the `ls` above, runs from the
`ubuntu` shell without `sudo`. `kubectl` there reads through the read-only kubeconfig.

1. `uname -r` prints a 7.0 kernel.
1. `ip -br addr show dev eno1` shows `10.0.0.215`. k3s pins `node-ip` to that address. If the
   link comes up under another name, the netplan pin misses it and k3s hangs at start; the
   comment on `node-ip` in `hosts/components/k3s-server/component.yml` describes the failure.
1. `systemctl is-active iscsid multipathd` prints `active` twice, and
   `lsmod | grep -E '^(iscsi_tcp|dm_multipath|wireguard) '` lists all three modules.
1. `kubectl get nodes` shows both nodes `Ready`.
1. Longhorn's node conditions match the baseline. Run the command below and compare it with
   daniel-server's lines. Before the upgrade, both nodes read `True` for every condition except
   two. `KernelModulesLoaded` is `False` and names `dm_crypt`, which no volume uses.
   `Multipathd` is `False` with Longhorn's known `multipathd` warning, which
   `multipath-longhorn.conf` in the k3s-server component answers. A new
   `False` is the finding.

    ```bash
    kubectl -n longhorn-system get nodes.longhorn.io -o jsonpath='{range .items[*]}{.metadata.name}: {range .status.conditions[*]}{.type}={.status} {.message}{"\n"}{end}{end}'
    ```

### daniel-pi

- Run it from the LAN, not over WireGuard. wg-easy runs on the Pi.
- 162 MB of memory is available, plus 1.1 GB of free swap (zram and a swapfile). Expect the
  upgrade to take hours.
- The procedure's last daniel-pi step checks the boot migration, then merges the Docker PR
  below and follows it with `land.sh`. `land.sh` waits for the Pi to apply the package, so the
  PR merges only once the Pi's updater runs again. `homelab-docker-engine` then installs the
  resolute builds of the same versions and restarts dockerd, which restarts the Pi's
  containers.

## The Docker PR: move the Pi's Docker source to 26.04

Merge it only once daniel-pi runs 26.04. Merging earlier installs Docker packages built for
26.04 onto a 24.04 host.

- `hosts/components/apt-docker/docker.sources`: `Suites: resolute`, plus its comment.
- `hosts/components/pi-docker/component.yml`: `docker_engine_ubuntu_version: "26.04"`,
  `docker_engine_ubuntu_codename: resolute`, and `suite=resolute` in the four Renovate
  `registryUrl`s.
- `scripts/validate/validate_lib/repos_case.py`: the expected `Suites: resolute`.

## The cleanup PR: retire 24.04

Open it once all three hosts run 26.04.

- Drop the 24.04 leg from `host_packages` in `.github/workflows/ci.yml` and from
  `.github/workflows/host-test-image.yml`. Also drop it from `image.pins`, the `PUBLISHED` set,
  the required contexts in `hosts/lib/ci_verdict.py` and the branch ruleset.
- Move `host_tools_arm64` and the arm64 image publisher to 26.04.
- Move the hypervisor guest image from `noble` to `resolute`. A rebuilt node's rollback
  target changes with it.
- Fix the comments that say the hosts run 24.04: `pi-tuning`, `homelab_rootq.py` and its
  tests.

## Rollback

A host that fails the verify step stays stopped, with its updater timer masked. Its rollback
is a rebuild on 24.04 with `bootstrap-host.sh`, as the procedure says. Until the cleanup PR
merges, CI keeps testing every package on 24.04, so the rebuild has a tested target.
