---
generated_from: scripts/gen_reference_networking.py
generated_at: 2026-08-24 20:21 UTC
generated_sha: f24a3c33
---

!!! warning "Generated file — do not edit"
    This page is rendered from the Ansible tree by `scripts/gen_reference_networking.py`. Hand edits are
    overwritten by the next run, and a prek hook rejects them at commit time.
    To change what appears here, change the generator or the source it reads.


# Networking

33 routed k8s service(s).

!!! note "Hostname labels, not FQDNs"
    The route is built as `<label>.local.<domain>`, and `domain` is SOPS-sourced with no static default. These pages are rendered by static parsing, so the label is printed and the suffix is not guessed at.


4 route(s) are LAN-only. Everything else becomes publicly resolvable once `k8s_public_route` is set — the absent Host rule is the guard, not DNS, because the Cloudflare wildcard resolves any name.


## Routes

| Service | Host | Hostname label | Reachable from | Middlewares |
|---|---|---|---|---|
| artifacts | daniel-box | `artifacts` | LAN + public (when k8s_public_route) | `rate-limit`, `authelia` |
| authelia | daniel-box | `auth` | LAN + public (when k8s_public_route) | `rate-limit` |
| bazarr | daniel-box | `bazarr` | LAN + public (when k8s_public_route) | `rate-limit`, `authelia` |
| bento-pdf | daniel-box | `bento-pdf` | LAN + public (when k8s_public_route) | `rate-limit`, `authelia` |
| claude-otel | daniel-box | `grafana` | LAN + public (when k8s_public_route) | `rate-limit`, `authelia` |
| code-server | daniel-box | `code-server` | LAN + public (when k8s_public_route) | `rate-limit`, `authelia` |
| crowdsec | daniel-box | `crowdsec-lapi` | LAN only | `rate-limit` |
| docs | daniel-box | `docs` | LAN only | `rate-limit`, `authelia` |
| freshrss | daniel-box | `freshrss` | LAN + public (when k8s_public_route) | `rate-limit`, `authelia` |
| headlamp | daniel-box | `headlamp` | LAN + public (when k8s_public_route) | `rate-limit`, `authelia` |
| healthchecks | daniel-box | `healthchecks` | LAN + public (when k8s_public_route) | `rate-limit`, `authelia` |
| home-assistant | daniel-box | `home-assistant` | LAN + public (when k8s_public_route) | `rate-limit` |
| homepage | daniel-box | `homepage` | LAN + public (when k8s_public_route) | `rate-limit`, `authelia` |
| ical-proxy | daniel-box | `ical-proxy` | LAN + public (when k8s_public_route) | `rate-limit` |
| jellyfin | daniel-box | `jellyfin` | LAN + public (when k8s_public_route) | `rate-limit` |
| karakeep | daniel-box | `karakeep` | LAN + public (when k8s_public_route) | `rate-limit`, `authelia`, `csp-karakeep` |
| littlelink | daniel-box | `www` | LAN + public (when k8s_public_route) | `rate-limit` |
| loki-homelab | daniel-box | `loki-homelab` | LAN + public (when k8s_public_route) | `rate-limit` |
| longhorn-ui | daniel-box | `longhorn` | LAN only | `rate-limit`, `authelia` |
| n8n | daniel-box | `n8n` | LAN + public (when k8s_public_route) | `rate-limit`, `authelia` |
| peanut | daniel-box | `peanut` | LAN + public (when k8s_public_route) | `rate-limit`, `authelia` |
| pihole | daniel-box | `pihole` | LAN + public (when k8s_public_route) | `rate-limit`, `authelia` |
| prowlarr | daniel-box | `prowlarr` | LAN + public (when k8s_public_route) | `rate-limit`, `authelia` |
| qbittorrent | daniel-box | `qbittorrent` | LAN + public (when k8s_public_route) | `rate-limit`, `authelia` |
| radarr | daniel-box | `radarr` | LAN + public (when k8s_public_route) | `rate-limit`, `authelia` |
| scrutiny | daniel-box | `scrutiny` | LAN + public (when k8s_public_route) | `rate-limit`, `authelia` |
| sonarr | daniel-box | `sonarr` | LAN + public (when k8s_public_route) | `rate-limit`, `authelia` |
| speedtest | daniel-box | `speedtest` | LAN + public (when k8s_public_route) | `rate-limit`, `authelia` |
| tdarr | daniel-box | `tdarr` | LAN + public (when k8s_public_route) | `rate-limit`, `authelia` |
| traefik | daniel-box | `traefik` | LAN only | `rate-limit` |
| uptime-kuma | daniel-box | `uptime-kuma` | LAN + public (when k8s_public_route) | `rate-limit`, `authelia` |
| wg-easy | daniel-box | `wg-easy` | LAN + public (when k8s_public_route) | `rate-limit`, `authelia` |
| zigbee2mqtt | daniel-box | `zigbee2mqtt` | LAN + public (when k8s_public_route) | `rate-limit`, `authelia` |

## Reading the middleware column

`rate-limit` is applied by the shared macro to every route. `authelia` is present when the inventory entry sets `use_authelia: true`, and it is what makes a request meet the SSO gate. **An Authelia redirect fires in the middleware, before Traefik proxies to the workload** — so a 302 from a route proves the edge is up, and nothing about whether the backend is healthy.
