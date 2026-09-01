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
- **Granular tags:** `docker-repo` (APT repo + GPG + the cache-refresh/upgrade task),
  `docker-engine` (install + v1-wrapper removal), `docker-group` (user resolution +
  membership), `docker-daemon` (daemon.json + conditional restart), `docker-networks`.

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
2. **Install:** `docker-ce`, `-cli`, `containerd.io`, **and explicitly**
   `docker-compose-plugin` + `docker-buildx-plugin` (the engine behind
   `community.docker.docker_compose_v2` and its `build: always` — declared so they can't be
   dropped as auto-installed Recommends). Removes the deprecated linuxserver compose-v1 wrapper.
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
