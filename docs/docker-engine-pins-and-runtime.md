# Docker engine pins, the Go runtime limits and the teardown record

Working-out moved off `ansible/roles/setup/docker_install/CLAUDE.md` (#2993), which a session reads
on every touch of the last Docker host. The role doc keeps the rules; this page keeps the incident
that made the engine a held package, how the Renovate pins were derived, the measurements that
sized the Go runtime limits, and the uninstall the teardown arm answers.

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
recovery cron (#1910) restarts a stopped container; it cannot recreate one. That is why
`tasks/engine-upgrade.yml` brings every Compose project back with `recreate: always`.

## The manager tracks what apt installs, not what upstream tagged

Until 2026-09-24 the four pins read GitHub releases, and Docker packages a release days-to-weeks
after upstream tags it: PR #2327 offered containerd 2.4.0 while the index topped out at 2.3.5, so
`docker-engine-upgrade` would have failed at apt *after* stopping every Compose project on the Pi
(#2341). Reading `download.docker.com`'s own noble/arm64 index means the PR opens only once the Pi
can install the version.

One limit survives that. The manager strips the Debian revision apt carries, so `2.3.4-1` and `2.3.4-2` are
one version and a revision-only repackage offers no PR. ENFORCED by
`scripts/tests/test_renovate_docker_engine_pins.py::test_managers_name_the_apt_packages_they_pin`
and its siblings: the `depName` is the apt package the spec renders, the `registryUrl` is Docker's
index, and the group stays out of the `automerging` catch-all.

## The spec globs the Debian revision

`docker_install_apt_suffix` renders `-*~ubuntu.24.04~noble`, so a pin installs whichever revision
Docker published (#2357). It hardcoded `-1` until 2026-09-24, and that asserted a revision nothing
here verifies: the manager strips the revision, and Docker publishes several revisions of one
upstream version — `containerd.io 2.3.4-1~ubuntu.24.04~noble` and `2.3.4-2~ubuntu.24.04~noble` both
sit in the noble/arm64 index. A version published only at `-2`, a repackage superseding a withdrawn
`-1`, therefore rendered a spec apt cannot resolve. That blocks the fresh install and the pending
check that opens the deliberate upgrade, which are the two paths that install a pin at all.

`ansible.builtin.apt` glob-matches the version in a `name=version` spec and hands `apt-get` the newest
match, so apt chooses the revision. Measured on daniel-pi 2026-09-24 in check mode:
`containerd.io=2.3.4-*~ubuntu.24.04~noble` built `apt-get --simulate install
'containerd.io=2.3.4-2~ubuntu.24.04~noble'`, the newer of the two revisions, while a spec naming a
revision the index lacks answered `E: Version '2.3.4-9~ubuntu.24.04~noble' for 'containerd.io' was
not found`. ENFORCED by
`ansible/tests/setup/test_docker_engine_is_held_before_apt_upgrade.py::test_the_apt_spec_globs_the_debian_revision`.

**Cost accepted:** the pending check reads a revision-only repackage as pending, so a run of the
deliberate upgrade stops and recreates every Compose project to move `-1` to `-2`. No Renovate PR
announces that, and nothing reaches the Pi until someone runs the play, so the cost lands only on a
run the operator already chose. The same shape covers the pin PRs themselves: merging one moves
nothing until the play runs, and the gap report covers every key of
`docker_install_package_versions`, one `dpkg-query` per package. It compared `docker-ce` alone until
2026-09-25, which left the other four visible only to `engine-upgrade.yml`'s apt probe (#2407).

## Why the teardown arm exists

Until 2026-08-17 the role was gated `when: has_docker` in `initial_setup.yml`, so flipping a host to
`has_docker: false` skipped it entirely and nothing declarative ever cleaned up. daniel-server's
2026-08-14 uninstall was done imperatively and missed a still-enabled
`docker-compose-qbittorrent.service` — `Requires=` a `docker.service` that no longer exists — plus
two crons for retired services. Install without uninstall is a one-way door; `tasks/teardown.yml` is
the way back out.

## The Go runtime limits, and what sized them

**The daemons serve their metrics on the LAN IP** (the `docker-daemon` tag): `metrics-addr` in
`daemon.json` on 9323, and `[metrics] address` in `/etc/containerd/config.toml` on 1338 at path
`/v1/metrics`, each with a UFW allow from `lan_subnet` — a host listener gets none of the bypass
Docker's own iptables chain gives a published port. The cluster's Prometheus scrapes them as
`dockerd-pi` and `containerd-pi` (`ansible/roles/k8s/claude-otel/`). A change here restarts
containerd, with no cascade into `dockerd`, and `live-restore` keeps the containers up.

They exist to size `GOMEMLIMIT`. On 2026-09-18 the two daemons took 97 and 53 major faults/s on
daniel-pi, 46% of the host's, each GC cycle faulting a swapped-out heap back from `zram` (#2003). The
readings beside `docker_install_gomemlimit_<daemon>` in `defaults/main.yml`: live heap 11–14 MB
each, 114 and 97 KB/s of allocation, 0.7 GC cycles a minute — above the two-minute forced-cycle
floor, so the heap goal was the trigger. 64 MiB gives each daemon a cycle every 350–430 s, the
cadence the Pi's Alloy runs at. `ansible/tests/setup/test_docker_daemons_gomemlimit_headroom.py`
refuses a limit whose slack is under two minutes of allocation, which is what the issue's
"live + 50%" recipe would have been on a heap this small.

**A child process inherits the environment.** containerd's shims get `os.Environ()`, so the
`config.toml` block sets `[plugins.'io.containerd.shim.v1.manager'] env = ["GOGC=100",
"GOMEMLIMIT=off"]`, which containerd appends after the inherited values and Go resolves last-wins;
that block carries the `docker-go-runtime-containerd` tag so the drop-in cannot land without it.
`dockerd`'s own `docker-proxy` processes — one per published port, four on the Pi — have no
such override and inherit the pairing. Accepted: they carry only loopback- and hairpin-origin
traffic and allocate close to nothing, and the day-after check reads their `VmRSS` (1.8 MB each
before). A shim or proxy started before the drop-in keeps its old environment until its container is
recreated.

**Applying it is by hand, one daemon per day**, so the day-after reading is attributable:

```
uv run ansible-playbook ansible/initial_setup.yml --tags docker-go-runtime-dockerd -e target=daniel-pi
```

then the same with `--tags docker-go-runtime-containerd`. Judge each the next day by
`rate(node_vmstat_pswpin{job="node-pi"}[1d])` against the 136–181/s of 2026-09-03 → 09-17, and by
the daemon's own `go_gc_duration_seconds_count` rate. An empty `docker_install_gomemlimit_<daemon>`
removes that daemon's drop-in and restarts it; `tasks/teardown.yml` removes both directories when a
host retires Docker.

## The deb822 repo migration

Commit `fee21f9` moved the APT repo to a deb822 `.sources` file, shared in shape with
`ansible/roles/setup/optimize_pi/`'s Log2Ram repo: both need `python3-debian`, and both clean up the
legacy one-line `.list`. `optimize_pi` already used the correct idiom — `get_url` with an explicit
`mode:` — and `docker_install` now matches it.

## The `default-address-pools` derivation

`daemon.json` pins `default-address-pools` to `10.200.0.0/16` in /24s. Docker's built-in default
(172.17-172.31/16 plus 192.168.0.0/16 in /20s) was nearly full, and new isolation networks had
started landing in 192.168.x — a common home-LAN range, and one Authelia, Unbound and Mullvad trust.
`10.200.0.0/16` is clear of the LAN, wg-easy (10.8/24), the Mullvad tunnel (10.64/10) and Docker's
own defaults. Only NEW networks draw from it; existing ones keep the addresses they have.
