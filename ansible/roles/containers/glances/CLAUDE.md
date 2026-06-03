# glances — System resource monitor

See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Image:** `nicolargo/glances:latest`
- **Host:** daniel-server · **Port:** 61208 · **URL:** `glances.<domain>` (Authelia: yes)
- **Networks:** monitoring
- **Depends on:** traefik, authelia
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- Lightweight live host view (CPU/mem/disk/net/containers); complements the
  Prometheus + Grafana stack with an at-a-glance UI. Also surfaced as a Homepage widget.

## Editing
- Compose: `templates/docker-compose.yml.j2`
- Deploy: `ansible-playbook ansible/deploy.yml --tags "glances"`
