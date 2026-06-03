# jellyfin — Media streaming server

See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Image:** `lscr.io/linuxserver/jellyfin:latest`
- **Host:** daniel-server · **Port:** 8096 · **URL:** `jellyfin.<domain>`
- **Authelia:** **no** — Jellyfin has its own auth and clients/apps can't pass Authelia 2FA
- **Networks:** media
- **Depends on:** traefik, authelia
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- **Intel iGPU hardware transcoding:** maps `/dev/dri` and loads the
  `linuxserver/mods:jellyfin-opencl-intel` mod.
- Publishes UDP `7359` (auto-discovery) and `1900` (DLNA/SSDP) on the host.
- Reads from the shared `data/media` library tree.

## Editing
- Compose: `templates/docker-compose.yml.j2`
- Deploy: `ansible-playbook ansible/deploy.yml --tags "jellyfin"`
