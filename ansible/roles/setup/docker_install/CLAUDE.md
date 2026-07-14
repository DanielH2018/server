# docker_install — Docker Engine + Compose v2 + the Docker networks

Installs Docker CE, the Compose/buildx plugins, the daemon config, and creates the shared
Docker networks every container role attaches to. **Not a container role** — a host-setup
role under `ansible/roles/setup/`, run by `initial_setup.yml`, not `deploy.yml`. See
repo-root `CLAUDE.md` and `.claude/rules/docker.md` for conventions.

## Where it runs
- In `ansible/initial_setup.yml`, after [[sops_setup]] — every host.
- `uv run ansible-playbook ansible/initial_setup.yml --tags "docker_install"`.
- **Granular tags:** `docker-repo` (APT repo + GPG + the cache-refresh/upgrade task),
  `docker-engine` (install + v1-wrapper removal), `docker-group` (user resolution +
  membership), `docker-daemon` (daemon.json + conditional restart), `docker-networks`.

## What it does (`tasks/main.yml`)
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
   off `apps`, Security M1), `kopia` (Kopia ↔ Traefik only — keeps the unauthenticated repo off
   other apps), `terraria` (Terraria ↔ Traefik only — the raw-TCP game route bypasses CrowdSec,
   so the container stays off `apps`), `ups` (NUT ↔ Home Assistant only), and `mqtt` (Mosquitto ↔
   Zigbee2MQTT ↔ Home Assistant only).

## Notable
- **`become: false` user resolution (task 3) is deliberate** — under the play's `become: true`,
  `ansible_facts.env.USER` is `root`; the user who actually runs `docker` is the unprivileged
  connecting user, so membership is resolved with `become: false`.
- **deb822 migration** (commit `fee21f9`) is shared with [[optimize_pi]]'s Log2Ram repo —
  both need `python3-debian` and both clean up the legacy `.list`.
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
