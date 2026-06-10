# bento-pdf — PDF generation / manipulation service

Self-hosted BentoPDF (browser-based PDF tools). See repo-root `CLAUDE.md`.

## At a glance
- **Image:** `ghcr.io/alam00000/bentopdf:latest`
- **Host:** daniel-server · **Port:** 8080 · **URL:** `bento-pdf.<domain>` (Authelia: yes)
- **Networks:** apps
- **Depends on:** traefik
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- Stateless tool container — no persistent data of note.

## Editing
- Compose: `templates/docker-compose.yml.j2`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "bento-pdf"`
