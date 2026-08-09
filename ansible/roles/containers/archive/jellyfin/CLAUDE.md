# jellyfin — Media streaming server

See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Image:** `lscr.io/linuxserver/jellyfin` (version-pinned, Renovate-managed)
- **Host:** daniel-server · **Port:** 8096 · **URL:** `jellyfin.<domain>`
- **Authelia:** **no** — Jellyfin has its own auth and clients/apps can't pass Authelia 2FA
- **Networks:** media
- **Depends on:** traefik (no Authelia — own auth)
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- **Intel iGPU hardware transcoding:** maps `/dev/dri` and loads the
  `linuxserver/mods:jellyfin-opencl-intel` mod.
- Publishes UDP `7359` (auto-discovery) and `1900` (DLNA/SSDP), bound to the
  host's LAN IP (`{{ server_ip }}`) rather than `0.0.0.0`.
- Reads from the shared `data/media` library tree — **both mounts read-only since
  2026-07-03** (SaveLocalMetadata is off everywhere, artwork/trickplay live in `/config`,
  and every writer has its own mount; trade-off: deleting media from the Jellyfin UI
  fails — delete via the *arrs) — mounted TWICE: at `/data` (the original
  mount the configured libraries point at — `/data/tv`, `/data/movies`) and at `/data/media`
  (janitorr-congruent view, 2026-07-02): janitorr's "Leaving Soon" collection dir and the
  symlink targets it writes are `/data/media/...` paths (janitorr's namespace), which only
  resolve in Jellyfin through the second mount. Don't remove either — dropping `/data`
  breaks the existing libraries (paths live in Jellyfin's own DB), dropping `/data/media`
  silently breaks Leaving Soon. Side effect: Docker auto-creates an empty root-owned
  mountpoint stub at `containers/data/media/media` on the host (nested-bind artifact) —
  expected, don't clean it up (it comes back on every recreate).

## Editing
- Compose: `templates/docker-compose.yml.j2`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "jellyfin"`
