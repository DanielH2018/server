# wg-easy — WireGuard VPN server + UI

WireGuard VPN with the wg-easy web admin, for remote access into the homelab.
**One host-agnostic role serves both daniel-server and daniel-pi** (the old separate
`wg-easy-pi` role was merged into this one). See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Image:** `ghcr.io/wg-easy/wg-easy:14@sha256:5f264…` — **pinned to the v14 tag + digest** (the
  bcrypt `PASSWORD_HASH` auth model). The `:14` keeps Renovate tracking the major (so it can offer a
  v15 bump as a deliberate PR); the digest keeps it immutable within v14.x. NOT `:latest`: v15 drops
  `PASSWORD_HASH` for an interactive setup wizard, which would silently reopen the server's
  monitoring-net-exposed admin API.
- **Hosts:** daniel-server AND daniel-pi
- **UI port:** 51821 · **URL:** `wg-easy.<domain>` (server, behind Authelia) / `http://<pi-lan-ip>:51821` (Pi)
- **WireGuard UDP port:** `udp_port` per host — **51820 on daniel-server, 51822 on daniel-pi**
  (both sit behind one public IP/router, so the listen ports must differ).
- **Networks:** monitoring (server) / proxy (Pi)
- **Depends on:** traefik, authelia (`meta/deps.yml`)
- **Config in:** each `ansible/inventory/host_vars/<host>.yml` → `containers_list`

## Notable
- **Exposure is host-driven** via `expose.yml.j2` + `expose_mode`: on the server
  (`expose_mode: traefik`) the UI is routed through Traefik behind Authelia and no host
  port is published; on the Pi (`expose_mode: lan`) the UI is published bound to the Pi's
  LAN IP and emits no Traefik labels. The WireGuard UDP port is always published on the host.
- **Server admin API is now authenticated (2026-07-04).** The server's `containers_list` entry
  carries `password_hash: "{{ wg_easy_password_hash }}"` (bcrypt, SOPS) → the compose `PASSWORD_HASH`
  env. This closes the admin UI/API that was otherwise **unauthenticated to every `monitoring`-net
  neighbor** (Authelia gates only the Traefik ingress, not the in-network `wg-easy:51821` path — a
  compromised monitoring-net app could mint a WireGuard peer, bypassing Authelia + CrowdSec). The
  bcrypt `$` is doubled in the template (`| replace('$', '$$')`) so Compose doesn't interpolate it.
- **Pi UI is unauthenticated on the trusted LAN (accepted risk, 2026-06-23).** The Pi instance
  has no Authelia (LAN-only host) AND its entry omits `password_hash`, so anyone on the Pi's LAN can
  open `http://<pi-lan-ip>:51821` and mint WireGuard client configs (i.e. VPN access into the
  homelab). **Accepted:** the Pi is never internet-exposed and the UI is bound to the Pi's LAN IP
  (not 0.0.0.0, not the tunnel — see the exposure bullet). To gate it later, add a `password_hash`
  (SOPS) to the Pi's `containers_list` wg-easy entry, exactly like the server's.
- **Pi peer configs are backed up to Kopia (2026-07-04).** The Pi is otherwise out of Kopia scope,
  but its wg-easy `wg0.conf`/`wg0.json` (WireGuard private keys) can't be rebuilt by a redeploy. So
  this role installs a **daniel-server-only** daily cron (`/usr/local/bin/wg-easy-pull-pi-peers.sh`,
  23:30) that `sudo rsync`-pulls the Pi's `containers/wg-easy/config/` (root-owned `0600`/`0640`, so
  the Pi's NOPASSWD `sudo rsync` is required to read them) into `containers/wg-easy/pi-peers/` on the
  server — inside Kopia's snapshot source. Tasks are gated on `inventory_hostname == 'daniel-server'`
  (NOT `containers_list` — a tagged deploy filters that). See the kopia role's CLAUDE.md.
- **The pull is watchdogged (2026-07-05).** It uses **no `--delete`**, so a silently-failing pull
  (Pi unreachable, SSH/sudo break) leaves the last-good copy in place and the nightly Kopia snapshot
  still succeeds — **Backup Freshness would stay green while the un-rebuildable peer keys go stale**.
  So the script captures the rsync exit code + a `>=2` file-count floor and writes
  `/var/lib/wg-easy-pi-peers/state.json` (created sys_user-owned by this role's cron tasks);
  monitor-bridge bind-mounts it `:ro` and its `pi_peers` check pushes the **WG Pi Peer Backup** Kuma
  monitor — `down` on a failed pull, >2.5 d staleness, or missing state. The pulled `pi-peers/wg0.json`
  is also the monthly restore-drill sentinel for `wg-easy`. Deploy `wg-easy` before `monitor-bridge`
  on a fresh host so the state dir exists sys_user-owned. Push token `monitor_bridge_pi_peers_push_token`.
- **Built-in healthcheck:** the `wg-easy/wg-easy` image ships its own Docker `HEALTHCHECK`
  (`wg show | grep -q interface` — verifies the WireGuard *interface* is up, not just the
  UI), so there is no compose `healthcheck:` block. `autoheal` and uptime-kuma rely on this
  native status — don't add a redundant probe.

## Editing
- Compose: `templates/docker-compose.yml.j2`
- Deploy (server): `uv run ansible-playbook ansible/deploy.yml --tags "wg-easy"`
- Deploy (Pi, driven from the server): `uv run ansible-playbook ansible/deploy.yml --tags "wg-easy" -e target=daniel-pi`
  (`-e target=`, not `--limit` — the play's `hosts:` defaults to the local hostname, so
  `--limit daniel-pi` from the server matches zero hosts)
