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
  `com.centurylinklabs.watchtower.enable=false` (e.g. `crowdsec` dashboard, `wireguard`).
- Most images here are `:latest`, so Watchtower is the live-update path; pinned/built
  images (n8n, code-server, etc.) are updated via redeploy instead.

## Editing
- Compose: `templates/docker-compose.yml.j2`
- Deploy: `ansible-playbook ansible/deploy.yml --tags "watchtower"`
