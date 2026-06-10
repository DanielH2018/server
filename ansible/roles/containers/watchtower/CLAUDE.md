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
  and the version-pinned critical tier — traefik/authelia/kopia/pihole/*arr/jellyfin).
- Watchtower is the live-update path for the `:latest` long tail. The version-pinned
  critical tier is managed by **Renovate** (PRs through CI + the host health gate); locally
  built images (n8n, code-server, etc.) are updated via redeploy.

## Editing
- Compose: `templates/docker-compose.yml.j2`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "watchtower"`
