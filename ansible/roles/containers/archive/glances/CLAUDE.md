# glances — System resource monitor (RETIRED 2026-09-18)

> **Archived, not deployed.** No `containers_list` entry references this role, so nothing
> renders or deploys it. It is kept for the Compose plumbing, the way `archive/dozzle` is.
>
> **Why it went** (#2004). It held 66 MB of anonymous memory — 7 MB resident, 59 MB in
> zram, the largest single zram tenant — plus a containerd shim, on a 456 MB board that
> keeps 10-25 MB free, for load/memory/disk facts `node_exporter` already exports on the
> cluster's `node-pi` scrape job. Its last reader, monitor-bridge's Pi Pressure check,
> reads those series since PR #2038; the `Daniel Pi Glances` Kuma HTTP monitor and
> `probe.py pi <path>` went with it. The Pi memory budget review of 2026-09-18 has the
> numbers.
>
> **Its host artifacts were removed imperatively** at retire time — the container, its
> image and its `containers/glances/` Compose project directory. Nothing in Ansible
> recreates them.
>
> **To revive it**, restore the `containers_list` entry in `daniel-pi.yml`, move this role
> back to `roles/containers/`, and re-add `glances` to `EXPECTED_PUBLISHERS` in
> `ansible/tests/services/test_pi_publishing_containers.py`. The text below describes the
> role as it was deployed.

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
