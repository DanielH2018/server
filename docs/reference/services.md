---
generated_from: scripts/docs/service_catalog.py
generated_at: 2026-11-13 03:04 UTC
generated_sha: 62b768e65
---

!!! warning "Generated file — do not edit"
    This page is rendered from this repository by `scripts/docs/service_catalog.py`. Hand edits are
    overwritten by the next run, and a prek hook rejects them at commit time.
    To change what appears here, change the generator or the source it reads.


# Services

67 service(s) declared across 1 host(s).


## daniel-box

67 service(s).

| Service | Platform | Route | Auth | Backup tier | Auto-deploy |
|---|---|---|---|---|---|
| artifacts | k8s | <span class="fqdn" data-host="artifacts">artifacts.&lt;domain&gt;</span> · <span class="fqdn" data-host="artifacts.local">artifacts.local.&lt;domain&gt;</span> | Authelia one_factor | no PVC (stateless) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| authelia | k8s | <span class="fqdn" data-host="auth">auth.&lt;domain&gt;</span> · <span class="fqdn" data-host="auth.local">auth.local.&lt;domain&gt;</span> | none (public/no-auth) | daily -> R2 | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| autofix-bridge | k8s | no route (infra role) | unknown (use_authelia not declared on this entry) | no PVC (stateless) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| bazarr | k8s | <span class="fqdn" data-host="bazarr">bazarr.&lt;domain&gt;</span> · <span class="fqdn" data-host="bazarr.local">bazarr.local.&lt;domain&gt;</span> | Authelia one_factor | weekly -> B2 (default target); not Longhorn (media-local) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| bento-pdf | k8s | <span class="fqdn" data-host="bento-pdf">bento-pdf.&lt;domain&gt;</span> · <span class="fqdn" data-host="bento-pdf.local">bento-pdf.local.&lt;domain&gt;</span> | Authelia one_factor | no PVC (stateless) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| blackbox-exporter | k8s | no route (infra role) | unknown (use_authelia not declared on this entry) | no PVC (stateless) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| cloudflare-ddns | k8s | no route (infra role) | unknown (use_authelia not declared on this entry) | no PVC (stateless) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| cluster-base | k8s | no route (infra role) | unknown (use_authelia not declared on this entry) | no PVC (stateless) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| code-server | k8s | <span class="fqdn" data-host="code-server">code-server.&lt;domain&gt;</span> · <span class="fqdn" data-host="code-server.local">code-server.local.&lt;domain&gt;</span> | Authelia two_factor | no backup (listed in k3s_longhorn_nobackup_volumes); weekly -> B2 (default target) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| configarr | k8s | no route (infra role) | unknown (use_authelia not declared on this entry) | no PVC (stateless) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| coredns | k8s | no route (infra role) | unknown (use_authelia not declared on this entry) | no PVC (stateless) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| crowdsec | k8s | <span class="fqdn" data-host="crowdsec-lapi.local">crowdsec-lapi.local.&lt;domain&gt;</span> (LAN only) | none (public/no-auth) | no backup (listed in k3s_longhorn_nobackup_volumes) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| deploy-ui | k8s | <span class="fqdn" data-host="deploy.local">deploy.local.&lt;domain&gt;</span> (LAN only) | Authelia two_factor | no PVC (stateless) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| docs | k8s | <span class="fqdn" data-host="docs">docs.&lt;domain&gt;</span> · <span class="fqdn" data-host="docs.local">docs.local.&lt;domain&gt;</span> | Authelia one_factor | no PVC (stateless) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| dri-device-plugin | k8s | no route (infra role) | unknown (use_authelia not declared on this entry) | no PVC (stateless) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| flux | k8s | no route (infra role) | unknown (use_authelia not declared on this entry) | no PVC (stateless) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| freshrss | k8s | <span class="fqdn" data-host="freshrss">freshrss.&lt;domain&gt;</span> · <span class="fqdn" data-host="freshrss.local">freshrss.local.&lt;domain&gt;</span> | Authelia one_factor | weekly -> B2 (default target) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| game-stats | k8s | no route (infra role) | unknown (use_authelia not declared on this entry) | weekly -> B2 (default target) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| gpu-exporter | k8s | no route (infra role) | unknown (use_authelia not declared on this entry) | no PVC (stateless) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| headlamp | k8s | <span class="fqdn" data-host="headlamp">headlamp.&lt;domain&gt;</span> · <span class="fqdn" data-host="headlamp.local">headlamp.local.&lt;domain&gt;</span> | Authelia one_factor | no PVC (stateless) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| home-assistant | k8s | <span class="fqdn" data-host="home-assistant">home-assistant.&lt;domain&gt;</span> · <span class="fqdn" data-host="home-assistant.local">home-assistant.local.&lt;domain&gt;</span> | none (public/no-auth) | daily -> R2 | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| homelab-mcp | k8s | no route (infra role) | none (public/no-auth) | no PVC (stateless) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| homepage | k8s | <span class="fqdn" data-host="homepage">homepage.&lt;domain&gt;</span> · <span class="fqdn" data-host="homepage.local">homepage.local.&lt;domain&gt;</span> | Authelia one_factor | no PVC (stateless) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| ical-proxy | k8s | <span class="fqdn" data-host="ical-proxy.local">ical-proxy.local.&lt;domain&gt;</span> (LAN only) | none (public/no-auth) | no PVC (stateless) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| janitorr | k8s | no route (infra role) | unknown (use_authelia not declared on this entry) | not Longhorn (media-local) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| jellyfin | k8s | <span class="fqdn" data-host="jellyfin">jellyfin.&lt;domain&gt;</span> · <span class="fqdn" data-host="jellyfin.local">jellyfin.local.&lt;domain&gt;</span> | none (public/no-auth) | weekly -> B2 (default target); not Longhorn (media-local) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| karakeep | k8s | <span class="fqdn" data-host="karakeep">karakeep.&lt;domain&gt;</span> · <span class="fqdn" data-host="karakeep.local">karakeep.local.&lt;domain&gt;</span> | Authelia one_factor | no backup (StorageClass longhorn-nobackup); weekly -> B2 (default target) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| karma | k8s | <span class="fqdn" data-host="karma.local">karma.local.&lt;domain&gt;</span> (LAN only) | Authelia two_factor | no PVC (stateless) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| littlelink | k8s | <span class="fqdn" data-host="www">www.&lt;domain&gt;</span> · <span class="fqdn" data-host="www.local">www.local.&lt;domain&gt;</span> | none (public/no-auth) | no PVC (stateless) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| livesync | k8s | no route (infra role) | none (public/no-auth) | no backup (StorageClass longhorn-nobackup) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| loki-homelab | k8s | <span class="fqdn" data-host="loki-homelab.local">loki-homelab.local.&lt;domain&gt;</span> (LAN only) | none (public/no-auth) | no backup (StorageClass longhorn-nobackup) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| longhorn | k8s | no route (infra role) | unknown (use_authelia not declared on this entry) | no PVC (stateless) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| longhorn-backup | k8s | no route (infra role) | unknown (use_authelia not declared on this entry) | no PVC (stateless) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| longhorn-reconcile | k8s | no route (infra role) | unknown (use_authelia not declared on this entry) | no PVC (stateless) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| longhorn-restore-drill | k8s | no route (infra role) | unknown (use_authelia not declared on this entry) | no declared tier (runtime claim named by a shell variable) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| longhorn-ui | k8s | <span class="fqdn" data-host="longhorn.local">longhorn.local.&lt;domain&gt;</span> (LAN only) | Authelia two_factor | no PVC (stateless) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| media-volume | k8s | no route (infra role) | unknown (use_authelia not declared on this entry) | not Longhorn (media-local) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| metallb | k8s | no route (infra role) | unknown (use_authelia not declared on this entry) | no PVC (stateless) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| monitor-bridge | k8s | no route (infra role) | unknown (use_authelia not declared on this entry) | no PVC (stateless) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| mosquitto | k8s | no route (infra role) | unknown (use_authelia not declared on this entry) | no backup (StorageClass longhorn-nobackup) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| n8n | k8s | <span class="fqdn" data-host="n8n">n8n.&lt;domain&gt;</span> · <span class="fqdn" data-host="n8n.local">n8n.local.&lt;domain&gt;</span> | Authelia two_factor | weekly -> B2 (default target) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| navidrome | k8s | <span class="fqdn" data-host="navidrome">navidrome.&lt;domain&gt;</span> · <span class="fqdn" data-host="navidrome.local">navidrome.local.&lt;domain&gt;</span> | Authelia one_factor | weekly -> B2 (default target); not Longhorn (media-local) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| netpol-baseline | k8s | no route (infra role) | unknown (use_authelia not declared on this entry) | no PVC (stateless) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| node-exporter | k8s | no route (infra role) | unknown (use_authelia not declared on this entry) | no PVC (stateless) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| nut | k8s | no route (infra role) | unknown (use_authelia not declared on this entry) | no PVC (stateless) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| nut-exporter | k8s | no route (infra role) | unknown (use_authelia not declared on this entry) | no PVC (stateless) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| observability | k8s | <span class="fqdn" data-host="grafana">grafana.&lt;domain&gt;</span> · <span class="fqdn" data-host="grafana.local">grafana.local.&lt;domain&gt;</span> | Authelia one_factor | no backup (StorageClass longhorn-nobackup) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| peanut | k8s | <span class="fqdn" data-host="peanut">peanut.&lt;domain&gt;</span> · <span class="fqdn" data-host="peanut.local">peanut.local.&lt;domain&gt;</span> | Authelia one_factor | no PVC (stateless) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| pi-peer-backup | k8s | no route (infra role) | unknown (use_authelia not declared on this entry) | weekly -> B2 (default target) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| pihole | k8s | <span class="fqdn" data-host="pihole">pihole.&lt;domain&gt;</span> · <span class="fqdn" data-host="pihole.local">pihole.local.&lt;domain&gt;</span> | Authelia one_factor | unknown (PVC present, claim name not statically resolvable: {{ claim }}); no backup (StorageClass longhorn-nobackup) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| pihole-exporter | k8s | no route (infra role) | unknown (use_authelia not declared on this entry) | no PVC (stateless) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| prowlarr | k8s | <span class="fqdn" data-host="prowlarr">prowlarr.&lt;domain&gt;</span> · <span class="fqdn" data-host="prowlarr.local">prowlarr.local.&lt;domain&gt;</span> | Authelia one_factor | weekly -> B2 (default target) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| qbittorrent | k8s | <span class="fqdn" data-host="qbittorrent">qbittorrent.&lt;domain&gt;</span> · <span class="fqdn" data-host="qbittorrent.local">qbittorrent.local.&lt;domain&gt;</span> | Authelia one_factor | weekly -> B2 (default target); not Longhorn (media-local) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| radarr | k8s | <span class="fqdn" data-host="radarr">radarr.&lt;domain&gt;</span> · <span class="fqdn" data-host="radarr.local">radarr.local.&lt;domain&gt;</span> | Authelia one_factor | weekly -> B2 (default target); not Longhorn (media-local) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| registry | k8s | no route (infra role) | unknown (use_authelia not declared on this entry) | no backup (StorageClass longhorn-nobackup) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| scrutiny | k8s | <span class="fqdn" data-host="scrutiny">scrutiny.&lt;domain&gt;</span> · <span class="fqdn" data-host="scrutiny.local">scrutiny.local.&lt;domain&gt;</span> | Authelia one_factor | no backup (listed in k3s_longhorn_nobackup_volumes); weekly -> B2 (default target) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| sonarr | k8s | <span class="fqdn" data-host="sonarr">sonarr.&lt;domain&gt;</span> · <span class="fqdn" data-host="sonarr.local">sonarr.local.&lt;domain&gt;</span> | Authelia one_factor | weekly -> B2 (default target); not Longhorn (media-local) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| speedtest | k8s | <span class="fqdn" data-host="speedtest">speedtest.&lt;domain&gt;</span> · <span class="fqdn" data-host="speedtest.local">speedtest.local.&lt;domain&gt;</span> | Authelia one_factor | no backup (StorageClass longhorn-nobackup) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| system-upgrade | k8s | no route (infra role) | unknown (use_authelia not declared on this entry) | no PVC (stateless) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| tdarr | k8s | <span class="fqdn" data-host="tdarr">tdarr.&lt;domain&gt;</span> · <span class="fqdn" data-host="tdarr.local">tdarr.local.&lt;domain&gt;</span> | Authelia one_factor | weekly -> B2 (default target); not Longhorn (media-local) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| terraria | k8s | no route (infra role) | unknown (use_authelia not declared on this entry) | weekly -> B2 (default target) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| texbrain | k8s | <span class="fqdn" data-host="texbrain">texbrain.&lt;domain&gt;</span> · <span class="fqdn" data-host="texbrain.local">texbrain.local.&lt;domain&gt;</span> | Authelia one_factor | no PVC (stateless) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| traefik | k8s | no route (infra role) | unknown (use_authelia not declared on this entry) | daily -> R2 | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| traefik-crds | k8s | no route (infra role) | unknown (use_authelia not declared on this entry) | no PVC (stateless) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| valheim | k8s | no route (infra role) | unknown (use_authelia not declared on this entry) | weekly -> B2 (default target); no backup (StorageClass longhorn-nobackup) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| wg-easy | k8s | <span class="fqdn" data-host="wg-easy">wg-easy.&lt;domain&gt;</span> · <span class="fqdn" data-host="wg-easy.local">wg-easy.local.&lt;domain&gt;</span> | Authelia one_factor | weekly -> B2 (default target) | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |
| zigbee2mqtt | k8s | <span class="fqdn" data-host="zigbee2mqtt">zigbee2mqtt.&lt;domain&gt;</span> · <span class="fqdn" data-host="zigbee2mqtt.local">zigbee2mqtt.local.&lt;domain&gt;</span> | Authelia one_factor | daily -> R2 | n/a: applied by Flux, through `promote`; `land.sh` gates the rollout |

## Underivable facts

32 field(s) read `unknown`. A fact with no machine-readable source prints its reason rather than a guess — see the FIELD NOTES section of `scripts/docs/service_catalog.py` for which facts those are and why.
