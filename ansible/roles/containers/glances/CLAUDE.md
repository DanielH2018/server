# glances — System resource monitor

See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Image:** `nicolargo/glances:latest`
- **Hosts:** daniel-server (Traefik + Authelia) · daniel-pi (LAN-bound, no Authelia)
- **Port:** 61208 · **URL:** `glances.<domain>` (server) / `http://<pi-lan-ip>:61208` (Pi)
- **Networks:** monitoring (server) · proxy (Pi)
- **Depends on:** traefik, authelia (server only)
- **Config in:** each `ansible/inventory/host_vars/<host>.yml` → `containers_list`

## Notable
- Lightweight live host view (CPU/mem/disk/net/containers); complements the
  Prometheus + Grafana stack with an at-a-glance UI. Also surfaced as a Homepage widget.
- **Host-agnostic exposure:** the template uses `expose.yml.j2` (`web_ui_labels` /
  `web_ui_ports_block`) so it renders Traefik+Authelia labels on the server (`expose_mode:
  traefik`) and a LAN-bound port on hosts with `expose_mode: lan` (daniel-pi). It runs on
  both hosts where listed in `containers_list`.

## Editing
- Compose: `templates/docker-compose.yml.j2`
- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "glances"`
