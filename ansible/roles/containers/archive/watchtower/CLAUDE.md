# watchtower — Automatic Docker image updates

Polls for newer image tags and recreates containers. See repo-root `CLAUDE.md`.

## At a glance
- **Image:** `nickfedor/watchtower:latest`
- **Host:** daniel-server · **No web UI**, no Authelia
- **Networks:** lifecycle only (talks to `docker-proxy-lifecycle`, not the broad nets)
- **Depends on:** docker-proxy
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- Services that must not auto-update opt out with label
  `com.centurylinklabs.watchtower.enable=false` (e.g. `crowdsec` dashboard, `wireguard`,
  and the version-pinned critical tier — traefik/authelia/kopia/pihole/*arr/jellyfin/
  livesync(couchdb)/uptime-kuma/mosquitto/portainer).
- Watchtower is the live-update path for the `:latest` long tail. The version-pinned
  critical tier is managed by **Renovate** (PRs through CI + the host health gate); locally
  built images (n8n, code-server, etc.) are updated via redeploy. The opted-out
  **mutable-tag** tier (wireguard, qbittorrent, scrutiny, crowdsec, unbound, flaresolverr)
  updates ONLY via `deploy.yml --tags <svc> -e common_pull=always` — a plain redeploy never
  re-pulls a locally-present tag (see `common/tasks/docker_deploy.yml`).
- **Built-in healthcheck (no compose `healthcheck:` block by design).** The
  `nickfedor/watchtower` image ships its own Docker `HEALTHCHECK` (`/watchtower --health-check`),
  so `docker ps` reports health without a compose probe — same pattern as `authelia`/`homepage`.
  Don't add a redundant `healthcheck()` block (a "which services lack a healthcheck" grep flags it;
  it's already covered).

## Editing
- Compose: `templates/docker-compose.yml.j2`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "watchtower"`
