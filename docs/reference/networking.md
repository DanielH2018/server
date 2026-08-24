---
generated_from: scripts/gen_reference_networking.py
generated_at: 2026-08-24 20:56 UTC
generated_sha: 747290ae
---

!!! warning "Generated file — do not edit"
    This page is rendered from the Ansible tree by `scripts/gen_reference_networking.py`. Hand edits are
    overwritten by the next run, and a prek hook rejects them at commit time.
    To change what appears here, change the generator or the source it reads.


# Networking

33 routed k8s service(s).

!!! note "The domain is filled in by your browser"
    `domain` is SOPS-sourced with no static default, and these pages are rendered by static parsing, so the generator writes `<domain>` rather than guessing. On the docs site the routes below become links, built from the domain of the URL you are reading this on — so you get LAN links on the LAN name and public links on the public one.


4 route(s) are LAN-only, and the rest answer on both names. The absent Host rule is what keeps a LAN-only route off the internet, not DNS — the Cloudflare wildcard resolves any name.


## Routes

| Service | Host | Route | Reachable from | Middlewares |
|---|---|---|---|---|
| artifacts | daniel-box | <span class="fqdn" data-host="artifacts">artifacts.&lt;domain&gt;</span> · <span class="fqdn" data-host="artifacts.local">artifacts.local.&lt;domain&gt;</span> | LAN + public | `rate-limit`, `authelia` |
| authelia | daniel-box | <span class="fqdn" data-host="auth">auth.&lt;domain&gt;</span> · <span class="fqdn" data-host="auth.local">auth.local.&lt;domain&gt;</span> | LAN + public | `rate-limit` |
| bazarr | daniel-box | <span class="fqdn" data-host="bazarr">bazarr.&lt;domain&gt;</span> · <span class="fqdn" data-host="bazarr.local">bazarr.local.&lt;domain&gt;</span> | LAN + public | `rate-limit`, `authelia` |
| bento-pdf | daniel-box | <span class="fqdn" data-host="bento-pdf">bento-pdf.&lt;domain&gt;</span> · <span class="fqdn" data-host="bento-pdf.local">bento-pdf.local.&lt;domain&gt;</span> | LAN + public | `rate-limit`, `authelia` |
| claude-otel | daniel-box | <span class="fqdn" data-host="grafana">grafana.&lt;domain&gt;</span> · <span class="fqdn" data-host="grafana.local">grafana.local.&lt;domain&gt;</span> | LAN + public | `rate-limit`, `authelia` |
| code-server | daniel-box | <span class="fqdn" data-host="code-server">code-server.&lt;domain&gt;</span> · <span class="fqdn" data-host="code-server.local">code-server.local.&lt;domain&gt;</span> | LAN + public | `rate-limit`, `authelia` |
| crowdsec | daniel-box | <span class="fqdn" data-host="crowdsec-lapi.local">crowdsec-lapi.local.&lt;domain&gt;</span> (LAN only) | LAN only | `rate-limit` |
| docs | daniel-box | <span class="fqdn" data-host="docs.local">docs.local.&lt;domain&gt;</span> (LAN only) | LAN only | `rate-limit`, `authelia` |
| freshrss | daniel-box | <span class="fqdn" data-host="freshrss">freshrss.&lt;domain&gt;</span> · <span class="fqdn" data-host="freshrss.local">freshrss.local.&lt;domain&gt;</span> | LAN + public | `rate-limit`, `authelia` |
| headlamp | daniel-box | <span class="fqdn" data-host="headlamp">headlamp.&lt;domain&gt;</span> · <span class="fqdn" data-host="headlamp.local">headlamp.local.&lt;domain&gt;</span> | LAN + public | `rate-limit`, `authelia` |
| healthchecks | daniel-box | <span class="fqdn" data-host="healthchecks">healthchecks.&lt;domain&gt;</span> · <span class="fqdn" data-host="healthchecks.local">healthchecks.local.&lt;domain&gt;</span> | LAN + public | `rate-limit`, `authelia` |
| home-assistant | daniel-box | <span class="fqdn" data-host="home-assistant">home-assistant.&lt;domain&gt;</span> · <span class="fqdn" data-host="home-assistant.local">home-assistant.local.&lt;domain&gt;</span> | LAN + public | `rate-limit` |
| homepage | daniel-box | <span class="fqdn" data-host="homepage">homepage.&lt;domain&gt;</span> · <span class="fqdn" data-host="homepage.local">homepage.local.&lt;domain&gt;</span> | LAN + public | `rate-limit`, `authelia` |
| ical-proxy | daniel-box | <span class="fqdn" data-host="ical-proxy">ical-proxy.&lt;domain&gt;</span> · <span class="fqdn" data-host="ical-proxy.local">ical-proxy.local.&lt;domain&gt;</span> | LAN + public | `rate-limit` |
| jellyfin | daniel-box | <span class="fqdn" data-host="jellyfin">jellyfin.&lt;domain&gt;</span> · <span class="fqdn" data-host="jellyfin.local">jellyfin.local.&lt;domain&gt;</span> | LAN + public | `rate-limit` |
| karakeep | daniel-box | <span class="fqdn" data-host="karakeep">karakeep.&lt;domain&gt;</span> · <span class="fqdn" data-host="karakeep.local">karakeep.local.&lt;domain&gt;</span> | LAN + public | `rate-limit`, `authelia`, `csp-karakeep` |
| littlelink | daniel-box | <span class="fqdn" data-host="www">www.&lt;domain&gt;</span> · <span class="fqdn" data-host="www.local">www.local.&lt;domain&gt;</span> | LAN + public | `rate-limit` |
| loki-homelab | daniel-box | <span class="fqdn" data-host="loki-homelab">loki-homelab.&lt;domain&gt;</span> · <span class="fqdn" data-host="loki-homelab.local">loki-homelab.local.&lt;domain&gt;</span> | LAN + public | `rate-limit` |
| longhorn-ui | daniel-box | <span class="fqdn" data-host="longhorn.local">longhorn.local.&lt;domain&gt;</span> (LAN only) | LAN only | `rate-limit`, `authelia` |
| n8n | daniel-box | <span class="fqdn" data-host="n8n">n8n.&lt;domain&gt;</span> · <span class="fqdn" data-host="n8n.local">n8n.local.&lt;domain&gt;</span> | LAN + public | `rate-limit`, `authelia` |
| peanut | daniel-box | <span class="fqdn" data-host="peanut">peanut.&lt;domain&gt;</span> · <span class="fqdn" data-host="peanut.local">peanut.local.&lt;domain&gt;</span> | LAN + public | `rate-limit`, `authelia` |
| pihole | daniel-box | <span class="fqdn" data-host="pihole">pihole.&lt;domain&gt;</span> · <span class="fqdn" data-host="pihole.local">pihole.local.&lt;domain&gt;</span> | LAN + public | `rate-limit`, `authelia` |
| prowlarr | daniel-box | <span class="fqdn" data-host="prowlarr">prowlarr.&lt;domain&gt;</span> · <span class="fqdn" data-host="prowlarr.local">prowlarr.local.&lt;domain&gt;</span> | LAN + public | `rate-limit`, `authelia` |
| qbittorrent | daniel-box | <span class="fqdn" data-host="qbittorrent">qbittorrent.&lt;domain&gt;</span> · <span class="fqdn" data-host="qbittorrent.local">qbittorrent.local.&lt;domain&gt;</span> | LAN + public | `rate-limit`, `authelia` |
| radarr | daniel-box | <span class="fqdn" data-host="radarr">radarr.&lt;domain&gt;</span> · <span class="fqdn" data-host="radarr.local">radarr.local.&lt;domain&gt;</span> | LAN + public | `rate-limit`, `authelia` |
| scrutiny | daniel-box | <span class="fqdn" data-host="scrutiny">scrutiny.&lt;domain&gt;</span> · <span class="fqdn" data-host="scrutiny.local">scrutiny.local.&lt;domain&gt;</span> | LAN + public | `rate-limit`, `authelia` |
| sonarr | daniel-box | <span class="fqdn" data-host="sonarr">sonarr.&lt;domain&gt;</span> · <span class="fqdn" data-host="sonarr.local">sonarr.local.&lt;domain&gt;</span> | LAN + public | `rate-limit`, `authelia` |
| speedtest | daniel-box | <span class="fqdn" data-host="speedtest">speedtest.&lt;domain&gt;</span> · <span class="fqdn" data-host="speedtest.local">speedtest.local.&lt;domain&gt;</span> | LAN + public | `rate-limit`, `authelia` |
| tdarr | daniel-box | <span class="fqdn" data-host="tdarr">tdarr.&lt;domain&gt;</span> · <span class="fqdn" data-host="tdarr.local">tdarr.local.&lt;domain&gt;</span> | LAN + public | `rate-limit`, `authelia` |
| traefik | daniel-box | <span class="fqdn" data-host="traefik.local">traefik.local.&lt;domain&gt;</span> (LAN only) | LAN only | `rate-limit` |
| uptime-kuma | daniel-box | <span class="fqdn" data-host="uptime-kuma">uptime-kuma.&lt;domain&gt;</span> · <span class="fqdn" data-host="uptime-kuma.local">uptime-kuma.local.&lt;domain&gt;</span> | LAN + public | `rate-limit`, `authelia` |
| wg-easy | daniel-box | <span class="fqdn" data-host="wg-easy">wg-easy.&lt;domain&gt;</span> · <span class="fqdn" data-host="wg-easy.local">wg-easy.local.&lt;domain&gt;</span> | LAN + public | `rate-limit`, `authelia` |
| zigbee2mqtt | daniel-box | <span class="fqdn" data-host="zigbee2mqtt">zigbee2mqtt.&lt;domain&gt;</span> · <span class="fqdn" data-host="zigbee2mqtt.local">zigbee2mqtt.local.&lt;domain&gt;</span> | LAN + public | `rate-limit`, `authelia` |

## Reading the middleware column

`rate-limit` is applied by the shared macro to every route. `authelia` is present when the inventory entry sets `use_authelia: true`, and it is what makes a request meet the SSO gate. **An Authelia redirect fires in the middleware, before Traefik proxies to the workload** — so a 302 from a route proves the edge is up, and nothing about whether the backend is healthy.
