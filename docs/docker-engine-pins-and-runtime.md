# Docker engine pins, the Go runtime limits and the teardown record

The Pi's Docker engine ships in `homelab-daniel-pi` since H7.26: `hosts/components/pi-docker/`
holds the pins, `homelab-docker-engine.service`, `daemon.json`, the Go-runtime drop-ins and
containerd's config block, and its `component.yml` header keeps the rules. This page keeps the
incident that made the engine a held package, how the Renovate pins were derived, the
measurements that sized the Go runtime limits, and the uninstall the teardown arm answers.

## How the engine moves

`homelab-docker-engine.service` is the one path that installs or moves the engine packages. An
upgrade stops every container for the apt run, so the unit starts two ways. An operator starts it
from a LAN session:

```bash
sudo systemctl start homelab-docker-engine.service
```

Or `homelab-docker-engine.timer` starts it at 00:30 UTC every night (operator ruling 2026-10-30,
H7 Open question 8). That slot follows the Pi's midnight cluster and precedes the updater's 01:05
resume, man-db and the 04:00 to 05:59 gap, which pi-peer-backup and the apt timers hold. The unit
does nothing while every package sits at its pin, so the timer acts only on the night after a pin
moved. `sudo systemctl mask --now homelab-docker-engine.timer` pauses it, and `unmask` then `start`
resumes it.

Its script, `/usr/lib/homelab/docker-engine`, takes one of three paths:

- With no `docker-ce` installed, as on a fresh Pi, it installs the pinned versions and holds them.
  `bootstrap-host.sh` starts the unit once for that case.
- With every package at its pin, it confirms the hold and exits 0. Nothing stops.
- Otherwise it masks `homelab-updater.timer` and waits out a running updater or reconcile, because
  both write the `homelab` project. It resolves every pin against `apt-cache madison` before
  anything stops, then stops the project, installs the pinned versions, holds them, starts the
  daemon and recreates every container with `--force-recreate`. It unmasks the timer last, and
  only when it masked it.

**A failed run leaves `homelab-updater.timer` masked**, so the Pi installs no package until the
operator runs `sudo systemctl unmask homelab-updater.timer` and
`sudo systemctl start homelab-updater.timer`. A failed nightly run does the same. The unit's `ExecStopPost=` re-holds every installed engine package whatever the outcome.

The package declares no `Depends` on the engine and ships no apt preferences pin, so the updater
can never move it. A merged pin PR changes only `/usr/lib/homelab/docker-engine.pins`, and the
updater then writes `homelab_docker_engine_pending{host="daniel-pi"} 1` beside its own series in
`homelab_host.prom` until the operator starts the unit. ENFORCED by
`hosts/lib/tests/test_pi_docker_component.py::test_the_package_cannot_move_the_engine_is_clean`
and `hosts/lib/tests/test_pi_docker_component.py::test_the_updater_reports_the_value_the_engine_script_prints`.

## The incident behind the hold

On 2026-09-18 `initial_setup`'s dist-upgrade replaced `containerd.io` 2.2.4→2.3.5 and `docker-ce`
29.5.3→29.8.1 on daniel-pi with every container running (#1961). `live-restore` kept the containers
up across the `dockerd` restart, but it does nothing for the containerd shim binary swapped under
them: autoheal's shim died (`failed to create TTRPC connection`), ssh refused connections for about
20 minutes on the 456 MB board, and `docker-proxy` sat `unhealthy` (`haproxy` 503) until a hand
redeploy recreated it.

The 503 is a stale bind-mount. `docker-proxy` mounts `/var/run/docker.sock` as a FILE, and a
`docker.socket` restart gives that path a new inode — measured on the Pi afterwards, the socket's
`ctime` equals `docker.socket`'s `ActiveEnterTimestamp` — which a running container never sees. The
recovery heartbeat (#1910) restarts a stopped container; it cannot recreate one. That is why
the engine unit recreates the `homelab` project with `--force-recreate` after apt, rather than
starting the stopped containers.

## The manager tracks what apt installs, not what upstream tagged

Until 2026-09-24 the four pins read GitHub releases, and Docker packages a release days-to-weeks
after upstream tags it: PR #2327 offered containerd 2.4.0 while the index topped out at 2.3.5, so
the Ansible-era upgrade play would have failed at apt *after* stopping every Compose project on the Pi
(#2341). Reading `download.docker.com`'s own noble/arm64 index means the PR opens only once the Pi
can install the version.

One limit survives that. The manager strips the Debian revision apt carries, so `2.3.4-1` and `2.3.4-2` are
one version and a revision-only repackage offers no PR. ENFORCED by
`scripts/tests/test_renovate_docker_engine_pins.py::test_managers_name_the_apt_packages_they_pin`
and its siblings: the `depName` is the apt package the spec renders, the `registryUrl` is Docker's
index, and the group stays out of the `automerging` catch-all.

## The spec globs the Debian revision

`hosts/components/pi-docker/templates/docker-engine.pins.j2` renders each pin as a glob ending
`-*~ubuntu.24.04~noble`, so a pin installs whichever revision Docker published (#2357). It hardcoded `-1` until 2026-09-24, and that asserted a revision nothing
here verifies: the manager strips the revision, and Docker publishes several revisions of one
upstream version — `containerd.io 2.3.4-1~ubuntu.24.04~noble` and `2.3.4-2~ubuntu.24.04~noble` both
sit in the noble/arm64 index. A version published only at `-2`, a repackage superseding a withdrawn
`-1`, therefore rendered a spec apt cannot resolve. That blocks the fresh install and the pending
check that opens the deliberate upgrade, which are the two paths that install a pin at all.

The engine script matches each glob against `apt-cache madison`, which lists the newest version
first, and hands `apt-get` the first match, so the newest published revision wins. The Ansible-era
play relied on `ansible.builtin.apt` doing the same match: measured on daniel-pi 2026-09-24 in
check mode, `containerd.io=2.3.4-*~ubuntu.24.04~noble` built `apt-get --simulate install
'containerd.io=2.3.4-2~ubuntu.24.04~noble'`, while a spec naming a revision the index lacks
answered `E: Version '2.3.4-9~ubuntu.24.04~noble' for 'containerd.io' was not found`. ENFORCED by
`hosts/lib/tests/test_pi_docker_component.py::test_a_fresh_host_installs_the_newest_matching_revision_and_holds_it`.

**Cost accepted:** a revision-only repackage does not read as pending, because the installed `-1`
still matches the glob. A run of the engine unit therefore leaves `-1` in place until an upstream
version moves. Merging a pin PR moves nothing until the unit runs, and the pending metric reads every
pinned package, one `dpkg-query` each. The Ansible-era report compared `docker-ce` alone until
2026-09-25, which left the other four invisible (#2407).

## Why the teardown arm exists

Install without uninstall is a one-way door, and `tasks/teardown.yml` is the way back out when a host flips to `has_docker: false`.

**HISTORY — the role was gated `when: has_docker` until 2026-08-17, so that flip skipped it and nothing declarative cleaned up.** daniel-server's imperative uninstall on 2026-08-14 missed a still-enabled `docker-compose-qbittorrent.service` and two crons for retired services.

## The Go runtime limits, and what sized them

**The daemons serve their metrics on the LAN IP**: `metrics-addr` in
`daemon.json` on 9323, and `[metrics] address` in `/etc/containerd/config.toml` on 1338 at path
`/v1/metrics`, each with a UFW allow from `lan_subnet` — a host listener gets none of the bypass
Docker's own iptables chain gives a published port. The cluster's Prometheus scrapes them as
`dockerd-pi` and `containerd-pi` (`ansible/roles/k8s/observability/`). Both files declare
`manual:` activation, so a change marks the daemon pending and restarts nothing.

They exist to size `GOMEMLIMIT`. On 2026-09-18 the two daemons took 97 and 53 major faults/s on
daniel-pi, 46% of the host's, each GC cycle faulting a swapped-out heap back from `zram` (#2003). The
readings behind `docker_engine_gomemlimit_<daemon>` in the component's defaults: live heap 11–14 MB
each, 114 and 97 KB/s of allocation, 0.7 GC cycles a minute — above the two-minute forced-cycle
floor, so the heap goal was the trigger. 64 MiB gives each daemon a cycle every 350–430 s, the
cadence the Pi's Alloy runs at. `ansible/tests/setup/test_docker_daemons_gomemlimit_headroom.py`
refuses a limit whose slack is under two minutes of allocation, which is what the issue's
"live + 50%" recipe would have been on a heap this small.

**A child process inherits the environment.** containerd's shims get `os.Environ()`, so the
`config.toml` block sets `[plugins.'io.containerd.shim.v1.manager'] env = ["GOGC=100",
"GOMEMLIMIT=off"]`, which containerd appends after the inherited values and Go resolves last-wins;
the block and the drop-in ship in the same package version, so neither lands without the other.
`dockerd`'s own `docker-proxy` processes — one per published port, four on the Pi — have no
such override and inherit the pairing. Accepted: they carry only loopback- and hairpin-origin
traffic and allocate close to nothing, and the day-after check reads their `VmRSS` (1.8 MB each
before). A shim or proxy started before the drop-in keeps its old environment until its container is
recreated.

**Applying a change is by hand, one daemon per day**, so the day-after reading is attributable.
Both daemons carry `policy: manual`, so the package installs the drop-in and restarts nothing. From
a LAN session, run `sudo /usr/lib/homelab/docker-engine restart containerd` one day and `sudo
/usr/lib/homelab/docker-engine restart docker` the next. Each restarts one daemon under the same
updater interlock the upgrade takes. A dockerd restart gives docker-proxy a stale socket inode, so
the `docker` form also recreates the project with `--force-recreate`. The same command applies a
`daemon.json` or containerd block change. Judge each the next day by `rate(node_vmstat_pswpin{job="node-pi"}[1d])` against the
136–181/s of 2026-09-03 → 09-17, and by the daemon's own `go_gc_duration_seconds_count` rate.
`docker_install`'s `tasks/teardown.yml` removes the Ansible-era drop-in directories when a host
retires Docker.

## The deb822 repo migration

The APT repo is a deb822 `.sources` file, the same shape as `ansible/roles/setup/optimize_pi/`'s
Log2Ram repo. Both need `python3-debian`, both fetch the key with `get_url` and an explicit `mode:`,
and both remove the legacy one-line `.list`.

## The `default-address-pools` derivation

`daemon.json` pins `default-address-pools` to `10.200.0.0/16` in /24s. Docker's built-in default
(172.17-172.31/16 plus 192.168.0.0/16 in /20s) was nearly full, and new isolation networks had
started landing in 192.168.x — a common home-LAN range, and one Authelia, Unbound and Mullvad trust.
`10.200.0.0/16` is clear of the LAN, wg-easy (10.8/24), the Mullvad tunnel (10.64/10) and Docker's
own defaults. Only NEW networks draw from it; existing ones keep the addresses they have.
