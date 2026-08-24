# glances — System resource monitor

See repo-root `CLAUDE.md` for shared conventions.

## At a glance
- **Image:** `nicolargo/glances:latest`
- **Hosts:** daniel-pi ONLY (LAN-bound, no Authelia)
- **Port:** 61208 · **URL:** `http://<pi-lan-ip>:61208`
- **Networks:** proxy

> **The daniel-server instance is gone.** Docker was uninstalled there on 2026-08-14 as the k3s
> migration's end state, and `host_vars/daniel-server.yml` now sets `containers_list: []`. The
> Traefik-routed, Authelia-gated `glances.<domain>` this file used to describe no longer exists;
> cluster-node metrics come from node-exporter and the observability stack instead.
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
