# daniel-box as master node — k3s migration design

**Date:** 2026-08-01

**Status: COMPLETE (2026-08-14).** The whole plan executed; Docker was uninstalled from
`daniel-server` on 2026-08-14, which is the migration's end state. Everything in this
directory is a **historical record of work already done** — read it for *why* a thing is
shaped the way it is, not as a description of pending work or of current architecture. For
current state, start at the repo-root [`README.md`](../../README.md) and
[`CLAUDE.md`](../../CLAUDE.md).

Phase-by-phase detail: `slice-7-phase-e-server-retirement.md` (server retirement),
then Phase F (daniel-server rejoined as a k3s agent) and Phase G (books close-out).

**Scope:** `daniel-server` (10.0.0.161) and `daniel-box` (10.0.0.215). `daniel-pi` explicitly out of scope.

**Repo:** `DanielH2018/server`

## 1. Decisions taken

| Decision | Choice | Rationale |
|---|---|---|
| Goal | Headroom + repurpose daniel-server + resilience | User-selected, all three |
| Orchestrator | **k3s** | Swarm reschedules containers but not data; every service here is bind-mounted local state |
| Control plane | **Single**, on daniel-box | Two nodes cannot form quorum; three managers would be needed |
| daniel-pi | Untouched | Stays LAN-only, Docker, `has_gitops: false` |
| Existing Docker stack | Runs throughout the migration | Per-service rollback, no big-bang cutover |
| Backup | **Longhorn's own backup target → B2** | Replaces Kopia for anything on a PV; Kopia retained for host paths |
| Pi-hole | **In-cluster, behind the VIP** | Makes the upstream `resolv.conf` rule mandatory (§7) |
| UPS / `peanut` | **Stays pinned to daniel-server** | Cable does not move; this is daniel-server's permanent residual role |
| Portainer | **Replaced by a k8s UI** | See the orphaned-Pi-agent consequence below |
| Third node | Possible later | Keeps `--cluster-init` worthwhile |

*Decisions confirmed 2026-08-01; design approved to proceed to an implementation plan.*

**Consequence of retiring Portainer — needs handling, not a decision.** Two things depend on it and both break:
- `homepage`'s Portainer widget queries `portainer:9000` and dies with it. Drop the widget from `services.yaml.j2`.
- **The Pi's `portainer-agent` is orphaned.** A k8s UI cannot manage the Pi's Docker daemon, so the agent, the `portainer_manager_host` flag, and the Pi's `DOCKER-USER` firewall rule admitting daniel-server all become dead weight. Default taken: retire all three together. This is a one-line `containers_list` removal on the Pi plus a firewall-rule cleanup — the only intrusion into the otherwise out-of-scope Pi, and it is a removal, not a change to what the Pi runs.

## 2. Verified current state

All figures measured on the hosts on 2026-08-01, not estimated.

**Hardware** — daniel-box is the stronger machine on both axes, which is what makes the promotion worth doing:

| | daniel-server | daniel-box |
|---|---|---|
| CPU | i5-1135G7, 4c/8t, 4.2 GHz | Ryzen 7 8845HS, 8c/16t, 5.1 GHz |
| iGPU | Intel Xe (QSV) | Radeon 780M (VAAPI) |
| Root FS | 455 G, 335 G free | 914 G, 864 G free |

**Footprint** — `/home/ubuntu/server/containers` totals **31 G**, of which `data/` (media + torrents) is **17 G**; the remainder (~14 G) is per-service config volumes. Some `kopia/` and `portainer/` subdirs were unreadable as `ubuntu`, so the true total is slightly higher. This is small enough that replicating config volumes across two nodes is cheap.

**Service count** — daniel-server's `containers_list` holds **46 active entries** (`grep -cE '^  - name:'`), plus `happy`, archived and commented out on 2026-07-19. daniel-box's `containers_list` is `[]` and nothing runs on it. `valheim` and `recyclarr` exist under `containers/` but are absent from the inventory — treat them as already dead, not as missed services.

**"Master node" already exists in this repo.** It is not a new concept to invent — it is four flags in `ansible/inventory/group_vars/all.yml`, all currently `daniel-server`:

- `monitoring_controller_host` — Uptime Kuma / push-heartbeat target
- `backup_controller_host` — pulls the Pi's wg-easy peer configs into Kopia scope
- `portainer_manager_host` — the host admitted through the Pi's `DOCKER-USER` firewall rule
- `renovate_notify_host` — posts the Renovate Discord digest; must be exactly one host

Promotion means flipping these four, and each has a downstream consequence (the Pi's firewall rule, the Kuma monitor topology).

## 3. The constraint that shapes everything

**Docker bridge networks are host-local.** The `networks:` lists in `containers_list` are not metadata — they are a same-host dependency graph resolved by container DNS. Services cannot be moved individually today; only whole clusters can move.

Worked examples from the current inventory:

- `monitor-bridge` sits on `monitoring`, `kopia`, `apps`, and `media`, reaching `prometheus`, `uptime-kuma`, `kopia`, `n8n:5678`, `home-assistant:8123`, `sonarr:8989`, `radarr:7878`
- `autofix-bridge` reaches `sonarr:8989`, `radarr:7878`, `uptime-kuma:3001`
- `homepage` queries `portainer:9000` directly — the `proxy` membership is load-bearing and a 2026-06-24 attempt to strip it broke the widget
- `zigbee2mqtt` and `home-assistant` both need `mosquitto`; HA also needs `upsd:3493`

`daniel-pi` is the existing precedent and confirms the rule: it runs a self-contained six-container set with `expose_mode: lan`, and its only cross-host link is a Portainer **agent** over a published TLS port, never container DNS.

**This is the single biggest thing k3s buys.** Cluster networking dissolves the constraint — after migration, services become individually schedulable. Everything else in this design is a consequence of getting there.

## 4. Target architecture

```
                    ┌─────────────────────────────────────┐
                    │  MetalLB VIPs (10.0.0.240-250)      │
                    │  ingress VIP · Pi-hole DNS VIP      │
                    └──────────────┬──────────────────────┘
                                   │
        ┌──────────────────────────┴──────────────────────────┐
        │                                                     │
┌───────▼────────────────────┐              ┌─────────────────▼──────────┐
│ daniel-box  10.0.0.215     │              │ daniel-server 10.0.0.161   │
│ k3s server (control plane) │◄────────────►│ k3s agent                  │
│ + worker                   │   Longhorn   │                            │
│                            │   replicas   │                            │
│ · media cluster (pinned)   │              │ · peanut/NUT (USB pinned)  │
│ · apps, monitoring         │              │ · scrutiny (DaemonSet)     │
│ · Traefik, Authelia        │              │ · spare capacity           │
└────────────────────────────┘              └────────────────────────────┘
```

**k3s flags on daniel-box:**

- `--cluster-init` — starts a single-member **embedded etcd** instead of the default SQLite. To be accurate about the stakes: this is *not* a one-way door. k3s documents the in-place conversion — "If you have an existing cluster using the default embedded SQLite database, you can convert it to etcd by simply restarting your K3s server with the `--cluster-init` flag" ([k3s docs](https://docs.k3s.io/datastore/ha-embedded), verified 2026-08-01). So this is convenience, not insurance: it costs nothing now and saves a control-plane restart later if a third server is ever added. Adopt it, but don't treat it as a decision that must be got right.
- `--disable=traefik` — the bundled Traefik cannot carry the existing config (CrowdSec Yaegi plugin, Authelia forwardauth, raw-TCP entrypoint for Terraria, Cloudflare `trustedIPs`). Own Traefik instead.
- `--disable=servicelb` — k3s's klipper-lb conflicts with MetalLB.

**Storage:**

- **Longhorn, 2 replicas** for config volumes (~14 G). This is what makes daniel-server's death survivable with data intact.
- **local-path + nodeAffinity** for the media library (17 G and growing). Replicating media is wasteful; it pins to daniel-box, which has 864 G free.

## 5. Service disposition

Not everything migrates. Budgeting this as 46 straight ports would be wrong. **This enumeration is complete** — every one of the 46 active entries lands in exactly one category below.

| Category | Count |
|---|---|
| Dissolves — replaced by a platform primitive | 5 |
| Reworks — real redesign | 8 |
| Pinned by hardware | 2 |
| Media set — moves as one unit, single-node | 9 |
| Migrate last — access/tooling hazard | 3 |
| Straight ports | 19 |
| **Total** | **46** |

> **"Straight port" still understates the work.** Every service's Traefik routing is label-driven through the `traefik.yml.j2` `labels()` macro (hostname, port, network, Authelia gate), and every monitor comes from the `autokuma.yml.j2` macro. **Both label systems cease to exist in k8s.** So each of ~40 services needs an IngressRoute and a Middleware authored by hand even when the workload itself ports cleanly. The one genuine freebie: `resources.yml.j2`'s `deploy.resources` caps map straight onto k8s requests/limits.

**Dissolves — replaced by platform primitives, not ported (5):**

| Service | Replacement |
|---|---|
| `docker-proxy` + `docker-proxy-codeserver` + `docker-proxy-lifecycle` | RBAC + ServiceAccounts |
| `autoheal` | liveness probes |
| `watchtower` | Renovate (already in this repo) + image automation |
| `portainer` | a k8s UI (Headlamp/Rancher), or retained only for Docker holdouts |
| `glances` | node-exporter / cAdvisor, already scraped by Prometheus |

Note `portainer_manager_host` and the Pi's `portainer-agent` are downstream of the Portainer decision.

**Reworks — real redesign, not a port (8):** `traefik`, `authelia`, `kopia`, `prometheus`, `grafana`, `otel-collector`, `uptime-kuma`, `terraria`.

- **Traefik + CrowdSec.** *(Done at slice-6 B1/B2 — this bullet is the pre-migration analysis;
  `slice-6-edge-cutover.md` is authoritative.)* The engine then lived in the `traefik` role, not the
  `crowdsec` role (a Metabase dashboard, since archived). Two predictions here were wrong in
  practice: acquisition did NOT go to `/var/log/pods` — each pod writes a file log to a shared
  emptyDir and carries its own agent sidecar (D2) — and the LAPI was not merely "rebuilt" but
  centralised, with the Docker engine demoted to one of four agents reporting to it.
- **Authelia** — forwardauth becomes a Middleware CRD. Its storage encryption key is a SOPS `pinned` DANGER secret.
- **AutoKuma** — reads Docker labels to generate Uptime Kuma monitors. Those labels do not exist in k8s. Either rework onto annotations or replace the monitor-generation path.
- **Prometheus / Grafana / OTel** — `kube-prometheus-stack` is the idiomatic target, but adopting it is a config rewrite, not a lift. Note **Loki has no `containers_list` entry of its own** — it is deployed from inside the `grafana` role's compose, so it migrates with Grafana rather than separately.
- **terraria** — served over a dedicated raw-TCP Traefik entrypoint on its own isolation net, deliberately outside the CrowdSec/rate-limit chain. That becomes a k8s `TCPRoute`/`IngressRouteTCP` plus a NetworkPolicy reproducing the isolation.
- **gitops_deploy** (not a `containers_list` entry, but in scope) — Flux/Argo, or keep the Ansible-driven apply (see §7).

**Node-pinned by hardware (2):**

| Service | Pin | Note |
|---|---|---|
| `peanut` (NUT) | daniel-server | `/dev/bus/usb` — **the UPS is physically cabled there.** Moving it is a cable move, not a config change |
| `scrutiny` | both | Collector becomes a DaemonSet so each node reads its own NVMe |

**Media set — moves as one unit (9):** `qbittorrent`, `sonarr`, `radarr`, `prowlarr`, `bazarr`, `jellyfin`, `tdarr`, `configarr`, `janitorr`. Pinned to daniel-box by the media PV. `jellyfin` and `tdarr` additionally take `/dev/dri` — Intel QSV → AMD VAAPI is not a no-op and transcode settings need revisiting.

**Migrate last — access or tooling hazard (3):**

| Service | Why last |
|---|---|
| `wg-easy` | UDP 51820 + `NET_ADMIN`, and it *is* the remote-access path — losing it mid-migration while away locks you out |
| `pihole` | LAN DNS; cold-boot dependency loop (see §7) |
| `homelab-mcp` | Serves the `mcp__homelab__*` tools used to diagnose the homelab — including during this migration. Don't remove the instrument while operating |

`otel-collector` sits in the rework list but shares the same hazard: it publishes OTLP to **host loopback specifically for Claude Code, which runs on the host, not in a container**. In k8s that needs `hostPort`/`hostNetwork` or the telemetry path breaks.

**Straight ports (19):** `crowdsec` (Metabase), `homepage`, `cloudflare-ddns`, `mosquitto`, `zigbee2mqtt`, `home-assistant`, `code-server`, `n8n`, `karakeep`, `freshrss`, `monitor-bridge`, `autofix-bridge`, `healthchecks`, `bento-pdf`, `littlelink`, `livesync`, `ical-proxy`, `speedtest`, `terraria-stats`.

Two caveats inside that list:
- The Zigbee stack is genuinely portable — the SLZB-06M coordinator is network-attached at `10.0.0.127`, not USB. `home-assistant` does reach `upsd:3493`, which lands on the pinned `peanut`; in k8s that becomes an ordinary cross-node Service call, so it stops being a co-location constraint.
- `homepage` queries `portainer:9000` directly. **If Portainer dissolves, that widget dies with it** — the two decisions are coupled.

## 6. Backup — the gap that must close first

**Kopia currently backs up a bind mount, and Longhorn will silently empty it.**

The `kopia` role mounts `/home/ubuntu/server/containers` read-only and pushes to **Backblaze B2**. When a service's config moves into a Longhorn PV, that host path still exists — it just no longer holds the live data. Kopia keeps reporting success while backing up nothing. This is a silent failure, and it lands on `backup_controller_host`, one of the four flags being flipped.

**Decided:** point **Longhorn's backup target at the same B2 bucket** (B2 exposes an S3-compatible API) for everything on a PV, and keep Kopia for what stays on host paths — the media library on local-path, plus host-level config. `docs/kopia-disaster-recovery.md` and the pinned kopia-password rotation procedure both need updating.

**Gate, applied to every slice:** a service's data must be visible in its *new* backup path before the Docker copy is decommissioned. No exceptions — this is the check that keeps a silent-backup failure from surviving the migration.

## 7. Networking, secrets, GitOps

**Ingress.** The router forwards 80/443 and 51820/UDP to `10.0.0.161`. Rather than re-pointing at `10.0.0.215` — which just moves the single point of failure — ingress gets a **MetalLB VIP**, so the address survives either node. The router forward flips once, at the end.

**Pi-hole is a cold-boot hazard, and it is going in-cluster.** It is LAN DNS. If it runs behind a VIP and the nodes resolve through it, a cold cluster start needs DNS — image pulls, Longhorn, MetalLB election — before DNS exists.

Since in-cluster is the decided path, the mitigation is no longer optional:

- The nodes' `/etc/resolv.conf` must point at an **upstream** resolver (e.g. 1.1.1.1), **never** at the VIP. This must be set *before* Pi-hole migrates, and it must survive reboots — on Ubuntu that means configuring `systemd-resolved` rather than hand-editing the symlinked `/etc/resolv.conf`.
- `systemd-resolved` binds `:53` on the node. Verify it does not conflict with the Pi-hole Service's VIP binding before slice 6, not during it.
- **Verification gate for slice 6:** cold-boot *both* nodes with the cluster down and confirm they reach a registry and come up without Pi-hole running. If that fails, Pi-hole falls back to Docker on daniel-server — that fallback stays available right up until the cutover, and taking it is not a failure.

**Secrets — no change to the workflow.** SOPS/age stays the source of truth. Ansible renders manifests with decrypted values and `kubectl apply`s them, exactly as it renders compose files today. daniel-box already decrypts (`d7495654`, `812dc069`), so there is no onboarding step. This deliberately avoids adding External Secrets or a SOPS operator during a migration that already has enough moving parts.

**Coexistence.** Both stacks run for weeks, so the inventory must stay one source of truth. Add a **`platform: docker|k8s` key per `containers_list` entry**, with `deploy.yml` branching on it. Each slice then becomes a one-line flip plus a manifest — instead of a parallel shadow inventory that drifts. This also keeps the existing machinery (deploy-tag derivation, the toposort ordering filter, `validate-compose`, the read-only `containers/` guard) coherent rather than half-bypassed.

## 8. Migration slices

Sequenced as thin end-to-end slices, each independently exercisable and reversible.

> **Superseded in one respect.** Slice 0 below installs k3s on both nodes. `slice-0-cluster-foundation.md` corrects this: k3s goes on **daniel-box alone** first, because daniel-server's Docker iptables chains, `DOCKER-USER` rules, and hairpin-NAT behaviour are the riskiest possible starting point. daniel-server joins at slice 7. Consequences: Longhorn runs at 1 replica until then, and the resilience payoff lands last. The plan document is authoritative where the two disagree.

| Slice | Content | Exit criterion |
|---|---|---|
| **0** | k3s on both nodes, `--cluster-init`, Longhorn, MetalLB pool, `platform:` key in `deploy.yml`. No service moved. | `kubectl get nodes` shows both Ready; a scratch PVC replicates |
| **1** | New Traefik + Authelia in k3s on a **new VIP**, plus one leaf service (`speedtest` or `bento-pdf`) behind a test hostname | Full chain verified: TLS, SSO, Kuma monitor, backup visible in B2 |
| **2** | Remaining leaf apps | Each reachable and backed up; Docker copies stopped |
| **3** | Monitoring cluster + bridges (incl. the AutoKuma rework) | Dashboards and alerts equivalent to today's |
| **4** | Media cluster (9 services) + iGPU transcode on daniel-box | A hardware-transcoded playback succeeds |
| **5** | Smart home: mosquitto, zigbee2mqtt, home-assistant | Zigbee mesh intact (PAN identity preserved); HA automations firing |
| **6** | Edge cutover: CrowdSec LAPI, Pi-hole, router forward → VIP, flip the four `*_host` flags | External access and LAN DNS on the new path |
| **7** | Drain Docker on daniel-server; decide its residual role | Only `peanut` + DaemonSets remain, or the UPS moves too |

**Slice 1 hazard — Authelia storage.** Do not run the k3s Authelia and the Docker Authelia against the same storage backend. Concurrent access corrupts sessions for both. The new instance gets its own storage.

**Slice 4 hazard — the classic \*arr footgun.** Sonarr, Radarr, and Bazarr store **absolute library paths in their databases**. Moving media from `/home/ubuntu/server/containers/data/media` onto a PV breaks every stored path unless the *in-container* mount point is preserved byte-for-byte. This is trivial to get right in k8s and catastrophic to get wrong — the symptom is a library that appears intact until an import silently fails. Fix it by pinning the container mount path, not by re-scanning.

**Slices 6-7 hazard — don't remove your own instruments.** `wg-easy` (remote access), `homelab-mcp` (diagnostics), and `otel-collector` (host-loopback OTLP for Claude Code) are the tools used to *operate* this migration. They go last, and `wg-easy` is a defensible candidate for never leaving Docker at all.

> **§8 CLOSE-OUT (2026-08-14) — every slice's exit criterion met; the migration is
> complete.** 0: both nodes Ready (daniel-box 08-02 solo per the plan correction;
> daniel-server joined 08-13), scratch-PVC gate passed at 2 replicas. 1–2: the leaf
> cohort served through the cluster edge with TLS/SSO/Kuma/backups from 08-05. 3: the
> monitoring cluster + bridge landed at Phase D (08-10), alert-equivalent and then
> better (per-check partition guards). 4: media moved as one hardlink-seam unit at B4c
> with a verified hardware-transcoded playback (08-08). 5: Zigbee mesh survived with
> PAN identity intact, HA automations firing (08-09). 6: the edge cutover finished at
> E7 (08-13) — external access, OIDC, and LAN DNS all cluster-served, proven by the
> Docker query-log flatline. 7: the drain completed 08-14 and its exit criterion was
> SUPERSEDED by the operator's D6 reversal — not "peanut + DaemonSets remain" but
> **nothing remains**: nut runs in-cluster on the USB host, Docker is uninstalled, and
> daniel-server is a k3s agent with a host shutdown chain. The instruments survived
> their own migration (wg-easy followed its router forwards at B5; homelab-mcp and the
> otel-collector moved/dissolved at Phase E and D7). Execution record:
> `slice-7-drain-and-join.md`.

## 9. Accepted limitations — stated plainly

- **Resilience is asymmetric.** daniel-server dies → workloads reschedule, Longhorn replicas intact. daniel-box dies → the API server is gone, nothing reschedules, recovery is manual. Longhorn buys data safety in that second case, not availability. This is inherent to a single control plane and was accepted knowingly.
- **The media cluster is single-node by construction.** Pinning media to local-path means all nine media services pin with it. That is the right call at 17 G and growing — but it is not nine services gaining failover.
- **Node-pinned services never fail over.** The UPS (USB), and each node's own SMART collector, are pinned by physics.
- **This is a months-long project.** 13 of the 46 services (5 dissolve + 8 rework) get redesigned rather than moved — and the remaining 33 each still need an IngressRoute and a Middleware hand-authored to replace the Traefik/AutoKuma label macros.

## 10. Decisions resolved

The first five closed on 2026-08-01; #6 closed 2026-08-14 after the drain surfaced it. Recorded with what each commits us to.

| # | Decision | Chosen | Commits us to |
|---|---|---|---|
| 1 | Backup | Longhorn backup target → B2 | Kopia's role shrinks to host paths; `docs/kopia-disaster-recovery.md` and the pinned password-rotation procedure need rewriting |
| 2 | Pi-hole | In-cluster, behind the VIP | The upstream `resolv.conf` rule and the cold-boot gate in §7 become mandatory work, not contingency |
| 3 | UPS / `peanut` | ~~Stays on daniel-server~~ **REVERSED 2026-08-14** (drain log "D6 REVERSED"): nut runs in-cluster, pinned to daniel-server for the USB | daniel-server fully drains — no residual Docker role; the host keeps only the k3s agent + the `nut_host` shutdown chain |
| 4 | Portainer | Replaced | Homepage widget removed; the Pi's agent, `portainer_manager_host`, and the Pi's `DOCKER-USER` rule retire together |
| 5 | Third node | Possible later | `--cluster-init` from day one; no other design change |
| 6 | L2/VIP pins *(added 2026-08-14, post-drain)* | **Permanent.** The L2Advertisement pin and the four VIP-backed workloads' (pihole/mosquitto/terraria/traefik) daniel-box nodeSelectors — born as incident response to the two ETP-Local blackouts — are the declared posture, not debt | daniel-box is a named SPOF for DNS/MQTT/edge (already true in practice); those four never fail over. In exchange ETP Local keeps real client IPs for CrowdSec + the ClientIP gates. The pins move only as one unit; `vip-kube-bypass` retired with the Docker uninstall (no forwarded VIP client left on the agent) |

**What's left is execution risk, not decision risk.** The two places this goes wrong *quietly* are the Kopia gap (§6) and the \*arr absolute paths (slice 4). Both are silent failures — they report success while being broken — and both have explicit gates written against them.

## 11. Unverified

- Docker's install/service state and sshd config contents on daniel-box were never independently checked — the green Ansible run is the only evidence (carried over from the bring-up handoff).
- Whether the jellyfin/tdarr templates encode QSV-specific transcode settings beyond the `has_igpu` flag.
- Whether the router's port-forward configuration is reachable/scriptable, or is a manual web-UI change.
