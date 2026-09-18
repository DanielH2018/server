# docker_install — Docker Engine + Compose v2 + the Docker networks

Installs Docker CE, the Compose/buildx plugins, the daemon config, and creates the shared
Docker networks every container role attaches to. **Not a container role** — a host-setup
role under `ansible/roles/setup/`, run by `initial_setup.yml`, not `deploy.yml`. See
repo-root `CLAUDE.md` and `.claude/rules/docker.md` for conventions.

## Where it runs
- In `ansible/initial_setup.yml`, after [[sops_setup]] — every host, **unconditionally**.
  `tasks/main.yml` is a dispatcher: `has_docker: true` runs `install.yml` (everything below),
  `has_docker: false` runs `teardown.yml`.
- `uv run ansible-playbook ansible/initial_setup.yml --tags "docker_install"`.
- **Granular tags:** `docker-repo` (APT repo + GPG + the cache refresh),
  `docker-engine` (install + hold + v1-wrapper removal), `docker-group` (user resolution +
  membership), `docker-daemon` (daemon.json + conditional restart), `docker-networks`,
  `docker-go-runtime` (both Go runtime drop-ins; `-dockerd` / `-containerd` select one).
  `docker-engine-upgrade` is `never`-tagged AND gated on `docker_install_engine_upgrade`: it runs only when named and opened with `-e` (below). `never` alone is not enough — the role tag inherits onto the include and overrides it (#1998).

## The engine is held; `--tags docker-engine-upgrade` is how it moves
`docker_engine_packages` (`group_vars/all.yml`: docker-ce, -cli, containerd.io, the compose
and buildx plugins) are `apt-mark hold`-equivalent on every `has_docker` host, set by
`ansible.builtin.dpkg_selections` in two places: [[initial_setup]]'s `host-basics.yml`
immediately BEFORE its dist-upgrade (this role runs after that role, so a hold set only here
would land one upgrade too late), and `install.yml` right after the install, for the fresh
host that had nothing to hold yet. Held, `apt-get upgrade`, `dist-upgrade` and
unattended-upgrades all leave them alone.

**Why.** On 2026-09-18 `initial_setup`'s dist-upgrade replaced containerd.io 2.2.4→2.3.5
and docker-ce 29.5.3→29.8.1 on daniel-pi with every container running (#1961). `live-restore`
kept the containers up across the dockerd restart, but it does nothing for the containerd
shim binary swapped under them: autoheal's shim died (`failed to create TTRPC connection`),
ssh refused connections for ~20 minutes on the 456 MB board, and docker-proxy sat
`unhealthy` (haproxy 503) until a hand redeploy recreated it. The 503 is a stale
bind-mount: docker-proxy mounts `/var/run/docker.sock` as a FILE, and a `docker.socket`
restart gives that path a new inode (measured on the Pi afterwards: the socket's ctime equals
`docker.socket`'s `ActiveEnterTimestamp`), which a running container never sees. The
recovery cron (#1910) restarts a stopped container; it cannot recreate one.

**The deliberate bump** (`tasks/engine-upgrade.yml`):
```
uv run ansible-playbook ansible/initial_setup.yml --tags docker-engine-upgrade -e docker_install_engine_upgrade=true -e target=daniel-pi
```
It refuses a host with no `~/server` checkout (nothing could stop or recreate the projects),
refreshes the cache, unholds, and asks the apt module in check mode whether `state: latest`
would change anything — the module's own verdict, not a parse of `apt-get -s`. Nothing
pending: re-hold and report. Otherwise it stops every Compose project in `containers_list`
(reverse order), upgrades, re-holds (in `always:`, so a failed apt run leaves the hold in
place), starts `docker.socket`/`docker.service`, and brings every project back with
`recreate: always` — the recreate the incident needed by hand, so docker-proxy gets the new
socket. Expect the Pi's containers, wg-easy included, to be down for the length of the apt
run. Run it from a LAN session, not over the tunnel.

**Cost accepted:** a Docker security fix waits for that command. The engine's upgrade
cadence is Renovate-free (no deb datasource is wired here); check `apt list --upgradable`
over ssh when the Renovate PRs for the Pi's images come round.

**Teardown unholds first.** apt with `-y` refuses to change a held package unless told
`--allow-change-held-packages`, so `teardown.yml` releases the hold before its purge.
ENFORCED by `ansible/tests/setup/test_docker_engine_is_held_before_apt_upgrade.py`: the
hold set is the install set, the hold precedes the dist-upgrade, and the teardown unholds.

## Teardown (`tasks/teardown.yml`, `has_docker: false`)
Reaps what an imperative Docker uninstall leaves behind: any `docker-compose-*.service`
unit (disable → remove → `daemon-reload`) and the crons in `docker_install_stale_crons`
whose owning Compose roles are archived and so can no longer reap them. Everything is
`state: absent`, so it is a no-op on a host that never had Docker.

**Why it exists:** until 2026-08-17 this role was gated `when: has_docker` in
`initial_setup.yml`, so flipping a host to `has_docker: false` skipped it entirely and
nothing declarative ever cleaned up. daniel-server's 2026-08-14 uninstall was done
imperatively and missed a still-enabled `docker-compose-qbittorrent.service`
(`Requires=` a `docker.service` that no longer exists) plus two crons for retired
services. Install without uninstall is a one-way door; this is the way back out.

**Not covered, deliberately:** package purge, `/var/lib/docker`, and the rendered
`containers/` tree. Those hold data, so removing them stays an operator decision.

## What it does (`tasks/install.yml`)
1. **APT repo (deb822):** installs prereqs (incl. `python3-debian`, required by
   `deb822_repository`), the Docker GPG key, and the Docker repo as a `.sources` file;
   removes any legacy one-line `docker.list` (the old `apt_repository` form is deprecated).
2. **Install:** `docker_engine_packages` — `docker-ce`, `-cli`, `containerd.io`, **and
   explicitly** `docker-compose-plugin` + `docker-buildx-plugin` (the engine behind
   `community.docker.docker_compose_v2` and its `build: always` — declared so they can't be
   dropped as auto-installed Recommends) — then HOLDS them (previous section). `state:
   present`, so an installed engine is never bumped by this task. Removes the deprecated
   linuxserver compose-v1 wrapper. The cache-refresh task before it carried `upgrade: true`
   until 2026-09-18 — a second full host upgrade on every run of this role; it refreshes only.
3. **docker group:** resolves the *connecting* user (not `root` under `become`) via `id -un`
   and appends them to the `docker` group.
4. **Daemon config** (`/etc/docker/daemon.json`): json-file log limits (10m × 3) +
   `live-restore: true` so a daemon restart (e.g. a `docker-ce` upgrade) doesn't bounce all
   ~58 containers, **+ `default-address-pools` (`10.200.0.0/16` in /24s)** — the built-in
   default pool (172.17-172.31/16 + 192.168.0.0/16 /20s) was nearly full and new isolation nets
   had started landing in 192.168.x (a common home-LAN range + the RFC1918 blocks
   Authelia/Unbound/Mullvad trust). `10.200.0.0/16` is clear of the LAN, wg-easy (10.8/24), the
   Mullvad tunnel (10.64/10), and Docker's own defaults; only NEW networks draw from it (existing
   ones keep their subnets). Restarts Docker only when the file changes.
5. **Networks:** creates `proxy` (`{{ docker_network }}`), `monitoring`, `media`, `apps`,
   `homepage_private`, `lifecycle` (Watchtower/Autoheal ↔ docker-proxy-lifecycle only),
   `codeserver` (code-server ↔ docker-proxy-codeserver only — lets the shared docker-proxy stay
   off `apps`, Security M1), `terraria` (Terraria ↔ Traefik only — the raw-TCP game route bypasses
   CrowdSec, so the container stays off `apps`), and `portainer-agent` (daniel-pi, single-member).
   `ups` and `mqtt` retired 2026-08-09 with slice-5 B3, `homepage_private` 2026-08-14, and `kopia`
   2026-08-27 — see the comments in `tasks/install.yml`, which is the list that decides.

## Notable
- **dockerd and containerd serve their metrics on the LAN IP** (`docker-daemon`):
  `metrics-addr` in `daemon.json` on 9323 and `[metrics] address` in
  `/etc/containerd/config.toml` on 1338 (path `/v1/metrics`), each with a UFW allow from
  `lan_subnet` — a host listener gets none of the bypass Docker's own iptables chain
  gives a published port. The cluster's Prometheus scrapes them as `dockerd-pi` and
  `containerd-pi` (`roles/k8s/claude-otel`). They exist to size the `GOMEMLIMIT` below:
  on 2026-09-18 the daemons took 97 and 53 major faults/s on daniel-pi, 46% of the
  host's, each GC cycle faulting a swapped-out heap back from zram (#2003). A change
  here restarts containerd (no cascade into dockerd; live-restore keeps the containers up).
- **Each daemon runs `GOGC=off` under a `GOMEMLIMIT`** (`tasks/go-runtime.yml`, one
  systemd drop-in per unit, sized at `docker_install_gomemlimit_<daemon>` in `defaults/main.yml`
  with the 2026-09-18 measurement beside it: live heap 11–14 MB each, 114 / 97 KB/s of
  allocation, 0.7 GC cycles a minute — above the two-minute forced-cycle floor, so the
  heap goal was the trigger). 64 MiB gives each a cycle every ~350–430 s, the cadence the
  Pi Alloy runs at; `ansible/tests/setup/test_docker_daemons_gomemlimit_headroom.py`
  refuses a limit whose slack is under two minutes of allocation, which is what the
  issue's "live + 50%" recipe would have been on a heap this small.
  **The environment is inherited by every child process.** containerd's shims get
  `os.Environ()`, so the config.toml block sets
  `[plugins.'io.containerd.shim.v1.manager'] env = ["GOGC=100", "GOMEMLIMIT=off"]`,
  which containerd appends after the inherited values and Go resolves last-wins; that
  block carries the `docker-go-runtime-containerd` tag so the drop-in cannot land without
  it. dockerd's userland `docker-proxy` processes (one per published port, four on the
  Pi) have no such override and inherit the pairing; accepted because they carry only
  loopback- and hairpin-origin traffic and allocate ~nothing — the day-after check reads
  their `VmRSS` (1.8 MB each before). A shim or proxy started before the drop-in keeps
  its old environment until its container is recreated.
  **Applying it is by hand, one daemon per day**, so the day-after reading is
  attributable: `uv run ansible-playbook ansible/initial_setup.yml --tags
  docker-go-runtime-dockerd -e target=daniel-pi` (then `…-containerd`), each judged the
  next day by `rate(node_vmstat_pswpin{job="node-pi"}[1d])` against the 136–181/s of
  2026-09-03 → 09-17 and by the daemon's own `go_gc_duration_seconds_count` rate. An
  empty `docker_install_gomemlimit_<daemon>` removes that daemon's drop-in and restarts
  it; `teardown.yml` removes both directories when a host retires Docker.
- **`become: false` user resolution (task 3) is deliberate** — under the play's `become: true`,
  `ansible_facts.env.USER` is `root`; the user who actually runs `docker` is the unprivileged
  connecting user, so membership is resolved with `become: false`.
- **The GPG key is fetched ASCII-armored to `/etc/apt/keyrings/docker.asc` and never
  dearmored.** apt reads armored keys referenced by `Signed-By` (this is what Docker's own
  install docs do). Do **not** reintroduce `gpg --dearmor` via `command`: that creates the
  file under root's umask, which [[initial_setup]] tightens to `027` *earlier in the same
  play*. On a fresh host the keyring landed `0640`, apt fetches as the unprivileged `_apt`
  user, couldn't read it, and reported the repo as **unsigned** — failing `initial_setup.yml`
  at the cache refresh, one task before Docker would have installed (daniel-box, 2026-08-01).
  Existing hosts never showed it: their keyring predates the umask change and a `creates:`
  guard stopped it being rewritten, so the bug was invisible until the next fresh host.
  `ansible/tests/setup/test_apt_keyring_permissions.py` is the regression guard.
- **deb822 migration** (commit `fee21f9`) is shared with [[optimize_pi]]'s Log2Ram repo —
  both need `python3-debian` and both clean up the legacy `.list`. optimize_pi already used
  the correct idiom (`get_url` with an explicit `mode:`); this role now matches it.
- Networks are created here once; container roles only *attach* (see the `networks.yml.j2`
  macro). Adding a new shared network means editing the `loop:` here.
- **`live-restore` covers every container EXCEPT the `network_mode: service:wireguard` pair.** A
  daemon restart — a `docker-ce` upgrade OR **any** `daemon.json` edit in task 4 — keeps the ~63
  normal containers running, but re-triggers `docker-compose-qbittorrent.service`
  (`Requires=docker.service`, `Type=oneshot`), which re-runs `docker compose up -d` and RECREATES
  both `wireguard` + `qbittorrent` (the same boot-race unit the [[qbittorrent]] role documents). It
  self-heals (the wg0 listen-interface binding persists in `./config`), but after any daemon
  restart confirm the tunnel came back: `docker exec qbittorrent curl -s
  localhost:8080/api/v2/transfer/info` should show `dht_nodes` > 0 — a silent rebind to `eth0`
  stalls every torrent at 0% while the TCP-only healthcheck stays green (qbittorrent role's
  UDP-leak failure mode).
