# Two-Node NixOS + Orchestration — Design

**Date:** 2026-07-08
**Status:** Approved design; implementation planned per-phase (each phase gets its own spec→plan cycle).

## Context

The homelab is a mature Docker-Compose + Ansible fleet: ~44 service roles on `daniel-server`
(Intel NUC, Intel XE graphics, LVM) plus 6 lightweight agents on `daniel-pi` (Raspberry Pi
Zero 2 W, 512 MB, LAN-only). It has GitOps pull-based deploys, SOPS/age secrets, Traefik +
Cloudflare + Authelia + CrowdSec ingress, Kopia→Backblaze-B2 backups, and a
Prometheus/Grafana/Loki + Uptime-Kuma + monitor-bridge/autofix-bridge observability plane.

A new mini-PC has been acquired:

| Field | Spec |
| --- | --- |
| Model | GMKtec NucBox K8 Plus |
| CPU | AMD Ryzen 7 8845HS (8c/16t, 3.8 GHz) |
| GPU | AMD Radeon 780M (RDNA3 iGPU — VAAPI + AV1 encode) |
| Memory | 32 GB DDR5 (max 96 GB) |
| Storage | 1 TB SSD |
| Networking | 2× 2.5 GbE, WiFi 6, BT 5.2 |
| Ports | 1× OCuLink (PCIe4 x4), 2× USB-C, 4× USB-A, HDMI |

The K8 Plus is materially more capable than the current server.

## Goals

Add the K8 Plus as a second full node, and use the opportunity to **learn NixOS and later
Kubernetes**, while keeping a working homelab at every step and maintaining everything
cohesively in one git repo.

## Locked decisions

These were settled during brainstorming and frame the whole plan:

1. **Primary driver: learning-first.** The existing fleet is the curriculum. Optimize for
   pedagogical value; accept churn on the *new* node, but never break the working setup.
2. **Learning path: NixOS first, Kubernetes second.** Stacking both curves at once on top of a
   live migration is the burnout path. K8s is deferred to Phase 2.
3. **Config-management end-state: decide the server later.** The K8 Plus runs NixOS now;
   `daniel-server` stays Ubuntu+Ansible through Phase 1. The full end-state (server → NixOS)
   is revisited only once the new node has proven itself.
4. **Repo layout: monorepo, flake at root.** One repo, one CI, one SOPS keyring, one Renovate.
   Nix and Ansible share the *same* `ansible/vars/secrets.yml` via `sops-nix`.
5. **Runtime requirements: everything-as-code + blue-green + scaling.** Config-as-code is the
   plan's core (NixOS + sops-nix, already maximized). Blue-green deploys and horizontal scaling
   are Kubernetes capabilities that land in Phase 2, targeted **single-node for now** — learned as
   mechanics on the stateless service subset; multi-node HA stays a deferred Phase-3 option, not a
   commitment. See *Runtime requirements* below.

### Operator's stated (non-binding) vision

Long-term, the K8 Plus likely becomes the **primary node** — handling media workloads at a
minimum — with `daniel-server` ported to NixOS and folded into the monorepo. This is **not
committed**; it is the documented Phase-3 target, decided only after the new node runs well
standalone. The design below is built so that this future is *additive*, never a rewrite.

## Runtime requirements: config-in-code, blue-green, scaling

Three operator requirements and where each lands:

| Requirement | Kind | Phase | Notes |
| --- | --- | --- | --- |
| **Everything-as-code** | ✅ Plan core | 0–1 | NixOS declares host/services/firewall/users; sops-nix declares secrets (encrypted). Already maximized by the design. |
| **Blue-green deploys** | Kubernetes feature | 2 | NixOS gives atomic generation swap + rollback (host-level) — that is *not* blue-green. True blue-green needs traffic-shifting: k3s rolling updates, or Argo Rollouts / weighted Traefik-Gateway routing. Phase-1 oci-containers do not provide it. |
| **Service scaling** | Kubernetes feature | 2 | Replicas + load-balancing + autoscaling (HPA) are core k8s; podman/oci-containers have no scheduler for it. |

**Decided stance: single-node k3s for now.** Blue-green and scaling are learned as *mechanics* on
one node. Two limits are accepted honestly:

- **Single node is not HA.** One machine is a SPOF and caps scale at its own resources. Real
  availability — survive a node dying, scale past one box — needs ≥2 cluster nodes (the deferred
  Phase-3 multi-node option). Not a near-term goal.
- **Only the stateless subset applies.** DBs, the *arr apps, jellyfin, and Home Assistant are
  stateful singletons; they cannot be naively replicated or blue-greened (shared state, DB locks,
  coordinator affinity). Blue-green/scaling target the stateless services (`littlelink`,
  `bento-pdf`, `ical-proxy`, frontends).

**Phase-1 implication — keep oci-containers lean.** Since Phase 2 rewrites container definitions
as k8s manifests, do not gold-plate the `mk-oci` helper — build just enough to learn NixOS and run
the apps. The NixOS host/secrets/monitoring/backup/firewall work carries over fully; only the
container packaging is redone. This is the accepted cost of NixOS-first vs k8s-first.

## Hard constraint: storage anchoring

The media stack cannot be casually split across nodes. `sonarr`/`radarr`/`bazarr`/`prowlarr`/
`qbittorrent`/`jellyfin`/`tdarr` share a **single `/data` mount** so atomic-moves and hardlinks
work (see the `media-data-single-mount` convention; hardlinks and atomic-moves fail with
`EXDEV` across NFS or node boundaries). The library also lives on the server's LVM, far larger
than the K8 Plus's 1 TB SSD.

**Therefore:** media + bulk storage stay anchored to `daniel-server` for Phases 1–2. This is a
*Phase-1 stance*, not a permanent verdict — the operator's vision resolves it in Phase 3 by
relocating the storage/filesystem to the K8 Plus (OCuLink PCIe4 x4 → NVMe expansion, or a large
internal SSD). Any media move must relocate the **entire linked set together** onto one
filesystem and re-point Kopia.

`daniel-pi` is **explicitly out of scope**: a 512 MB Zero 2 W cannot practically run NixOS and
cannot run k3s at all. It stays on Ubuntu+Ansible.

## Target architecture

```
                    Internet ── Cloudflare ── (tunnel/DNS + CrowdSec)
                                     │
                          ┌──────────┴───────────┐  single ingress
                          │  Traefik (on server) │  Authelia forward-auth
                          └──────────┬───────────┘  stays central
              LAN 10.0.0.x ─────┬────┴─────┬─────────────────┐
                                │          │                 │
                    ┌───────────┴──┐  ┌────┴───────────┐  ┌──┴─────────┐
                    │ daniel-server│  │ K8 Plus (NEW)  │  │ daniel-pi  │
                    │ Ubuntu+Ansbl │  │    NixOS       │  │ Ubuntu+Ans │
                    │ MEDIA+STORAGE│  │  APPS + COMPUTE│  │ LAN-only,  │
                    │ *arr/jelly/  │  │  net-new svcs, │  │ 512MB, OUT │
                    │ tdarr/kopia/ │  │  later: k3s    │  │ of scope   │
                    │ HA hub       │  │                │  │            │
                    └──────────────┘  └────────────────┘  └────────────┘
```

**Division of labor:**
- `daniel-server` — media + bulk storage + fleet-central singletons (ingress, auth, monitoring).
- K8 Plus — the apps + compute + orchestration-learning node.
- `daniel-pi` — unchanged, out of scope.

## Phasing

- **Phase 0 — Bring-up.** NixOS on the K8 Plus; flake at repo root; `sops-nix` onboarded to the
  existing age keyring; node-exporter/cadvisor + Kopia + a Traefik file-provider route so the
  node joins the monitoring/backup/ingress planes on day 0. **Deliverable:** one trivial service
  reachable through the existing Traefik+Authelia, green in Kuma, scraped by Prometheus,
  rollback proven.
- **Phase 1 — Migrate the apps tier (Docker-on-NixOS).** Port services in the risk-ordered
  curriculum using `virtualisation.oci-containers`. Learn Nix's declarative idiom, sops-nix, the
  firewall, generations/rollback — without K8s in the mix. **End state:** the `apps`-network tier
  runs on NixOS.
- **Phase 2 — Kubernetes (k3s on NixOS).** Stand up single-node `services.k3s`; re-platform the
  apps tier again as k3s manifests. Orchestration learned on services already understood —
  including **blue-green deploys** and **horizontal scaling** on the stateless subset (single-node
  = mechanics, not HA).
- **Phase 3 — Decision gate (deferred).** Convert `daniel-server` to NixOS and/or add it as a
  real second k3s node for multi-node HA — the point at which blue-green/scaling become genuine HA
  rather than single-node mechanics; potentially relocate media/storage to make the K8 Plus
  primary. Explicitly not committed now.

## Tech choices

1. **Container mechanism (Phase 1): `virtualisation.oci-containers` with the podman backend.**
   Native NixOS module, rootless-capable, declarative per-container. Use **`compose2nix`** as an
   accelerator to convert existing compose templates, but hand-clean the output to actually learn
   the idiom.
2. **NixOS deploy mechanism: start local, graduate to colmena.** Begin with
   `sudo nixos-rebuild switch --flake .#k8plus` on the node (mirrors the current pull-based
   GitOps mental model; rollback via `--rollback` or an older generation is the learning safety
   net). Graduate to **`colmena apply --on k8plus`** pushed from the server-as-controller (reuses
   the existing SSH-to-Pi pattern). Not `deploy-rs` — colmena's model is simpler for a small
   fleet. Do **not** wire NixOS into `gitops_deploy` auto-deploy until Phase 1 is comfortable.
3. **K8s distro (Phase 2): k3s via `services.k3s` on NixOS.** Not Talos — Talos replaces NixOS as
   the host OS, discarding the Phase-0/1 NixOS learning. k3s-on-NixOS is the only choice coherent
   with "learn NixOS first."

## Repo layout (monorepo, flake at root)

```
flake.nix                 # inputs: nixpkgs, sops-nix, colmena(later); outputs: nixosConfigurations.k8plus
flake.lock
nix/
  hosts/
    k8plus/
      default.nix         # imports modules + this host's service list (the host_vars analogue)
      hardware-configuration.nix
    # (Phase 3) daniel-server/   ← additive, not a rewrite
  modules/
    common.nix            # ubuntu:1000/1000, TZ America/Chicago, ssh, nix settings, base firewall
    sops.nix              # sops-nix → ansible/vars/secrets.yml + host age key
    monitoring.nix        # node-exporter + cadvisor
    backup.nix            # kopia oci-container → same B2 repo
    services/<svc>.nix    # one per migrated service (mirrors ansible/roles/containers/<svc>)
  lib/
    mk-oci.nix            # macro analogue: traefik-route + kuma + healthcheck + podman defaults
ansible/  containers/  scripts/  docs/   # all unchanged
```

`nix/lib/mk-oci.nix` is the Nix answer to the `ansible/templates/*.j2` macros — one helper that
stamps the Traefik file-route, Kuma monitor, healthcheck, and podman defaults so per-service
files stay tiny (target ~10 lines each). Same discipline, new language.

## Cohesion plane

Each element reuses infrastructure already running.

- **Secrets — `sops-nix` on the *same* `ansible/vars/secrets.yml`.** The biggest cohesion win.
  Onboarding follows the existing bootstrap muscle memory: generate the K8 Plus age key → add its
  pubkey to `ansible/.sops.yaml` → `sops updatekeys ansible/vars/secrets.yml` (run from
  `ansible/`) → commit. Then Ansible's runtime lookup *and* Nix's sops-nix decrypt the identical
  file with the identical keyring. Secrets are decrypted at **activation** time into `/run/secrets/*`
  (tmpfs) — never written to the world-readable Nix store, never plaintext on disk. gitleaks +
  the `.sops.yaml` auto-encrypt already cover `vars/`; no new secret-scan gap.
- **Deploy — local first, in git.** `nixos-rebuild switch --flake .#k8plus`; rollback via
  `--rollback` / older generation. Graduate to `colmena apply` from the server. No auto-deploy
  until Phase 1 is comfortable — keep manual generations while learning.
- **CI / prek — additive hooks, mirroring the ruff discipline.** Add `nix flake check`, a
  formatter (`alejandra`), `statix` (lint) and `deadnix` (dead-code). CI gains a Nix install step
  (`DeterminateSystems/nix-installer-action` + magic-nix-cache), gated like the other jobs.
  Renovate's `nix` manager bumps `flake.lock` through the existing dependency dashboard.
- **Backup — Kopia on the new node, *same* B2 repo.** Mirror the `kopia` role as an oci-container
  with its own snapshot source (Kopia multi-host = one repo, different host path). Keep the
  `./data` bind-mount + anchored `.kopiaignore` conventions. Surface via the existing
  monitor-bridge push + a Kuma monitor.
- **Monitoring — join the existing planes on day 0.** node-exporter + cadvisor (declarative, Nix-
  native) → add scrape targets to the server's `prometheus.yml.j2` → add Kuma docker/push
  monitors (mind the instance-blind gotcha from the Pi notes — add node-level checks
  deliberately). monitor-bridge `check.py` already reasons over Prometheus targets, so new-node
  health surfaces once scraped.
- **Ingress — single Traefik, file-provider routes.** New-node services get routes via Traefik's
  file-provider **directory** (the inode-trap fix already mounts a directory) pointing at
  `http://10.0.0.<k8plus>:<port>` — Authelia forward-auth, CrowdSec, and rate-limit@file all still
  apply centrally. NixOS `networking.firewall` restricts those ports to LAN/server only
  (declarative firewall = a clean Nix lesson). The Phase-2 cluster-ingress choice is deferred.

## Service-migration curriculum

Risk-ordered so each rung teaches one new thing on top of the last, and nothing precious moves
before a restore is proven.

- **Rung 0 — Prove the loop** *(Phase 0 deliverable)*: `littlelink` (zero state). Exercises the
  whole pipeline: flake host → `mk-oci` → podman → Traefik file-route → Authelia → Kuma →
  Prometheus scrape → firewall. **Done when:** it loads through the existing ingress, is green in
  Kuma, scraped in Prometheus, and `nixos-rebuild --rollback` cleanly reverts.
- **Rung 1 — Stateless repetition** (harden the helper): `bento-pdf`, `ical-proxy`. Generalize
  `mk-oci.nix` until per-service files are ~10 lines. Learn multi-service hosts, firewall,
  Renovate flake bumps.
- **Rung 2 — First stateful** (volumes + backup + *restore*): `speedtest` (single container +
  DB) → `freshrss` (app + nginx sidecar = first multi-container unit). Learn persistent bind-
  mounts, sops-nix secret injection, a Kopia snapshot source on the new node, and a **restore
  drill** — the gate before anything precious moves.
- **Rung 3 — Heavier stateful/compute** (justify the Ryzen): `karakeep` (+chrome+meili — multi-
  sidecar, carries the meili version-pin policy), `n8n`, `livesync`/couchdb, `code-server`
  (replicate the Security-M1 dedicated-docker-proxy pattern in Nix). Teaches resource caps +
  trickier migrations.
- **Rung 4 — Optional/advanced** (only if 0–3 feel good): the home-automation cluster —
  `home-assistant` + `zigbee2mqtt` + `mosquitto`. *Movable* because the SLZB-06M coordinator is
  network-attached over TCP (`daniel-server.yml`), not USB-pinned — but high-touch given the
  automation/macro investment, so deliberately last. `terraria` too (CPU-hungry game server).

**Stays on `daniel-server` through Phases 1–2:**
- Singletons (anchor, don't migrate): `traefik`, `authelia`, `crowdsec`, `wg-easy`.
- Fleet monitoring hub: `prometheus`, `grafana`, `uptime-kuma`, `monitor-bridge`, `autofix-bridge`.
- Media + storage-anchored (hardlink constraint): `sonarr`/`radarr`/`bazarr`/`prowlarr`/
  `qbittorrent`/`jellyfin`/`tdarr`/`recyclarr`/`janitorr` + server `kopia`.
- Hardware-pinned: `scrutiny` (disk SMART), `peanut` (UPS/USB), `pihole` (Pi DNS).

**Obviated by NixOS (don't port — learn *why*):** `watchtower` (Nix + Renovate flake bumps
replace image auto-update), `autoheal` (systemd/podman native restart), `portainer-agent` (Nix is
the control plane). NixOS collapses several sidecars into the OS itself.

**Runs on both nodes, Nix-native (not oci):** node-exporter, cadvisor, optionally glances/dozzle.

## Phase 2 — Kubernetes (k3s), sketch

Re-platform Rung 0→2 as k3s objects on services already known cold: `littlelink`/`bento-pdf` as
Deployment + Service + Ingress; `speedtest`/`freshrss` with PVCs. Learn scheduling, probes,
ConfigMaps/Secrets (via a sops-nix→k8s bridge), and Ingress on familiar workloads.

**Blue-green + scaling are the headline Phase-2 lessons**, exercised on the stateless subset:
- **Scaling:** replicas + a Service load-balancer; then autoscaling (HPA + metrics-server) on
  `littlelink`/`bento-pdf`. Single node caps scale at the box's resources — that limit is accepted.
- **Blue-green:** start with k8s rolling updates (native), then a true blue-green/canary cut with
  **Argo Rollouts** (or weighted Traefik-Gateway routing) — hold two versions, shift traffic,
  roll back by flipping. Stateful singletons keep NixOS-generation-style deploys, not blue-green.

Cluster-ingress choice — NodePort into the existing Traefik vs k3s's bundled Traefik vs metallb
LoadBalancer — is decided at Phase 2. This phase gets its own spec before implementation.

## Phase 3 — Decision gate (deferred)

Revisited only after the K8 Plus runs well standalone. Options, non-binding:
- Convert `daniel-server` to NixOS: additive — add `nix/hosts/daniel-server/`, reuse the
  `services/<svc>.nix` modules already written in Phase 1.
- Make the K8 Plus primary + relocate media: requires a storage plan (OCuLink NVMe or large
  internal SSD), moving the entire linked media set to one filesystem, and re-pointing Kopia.
- Add `daniel-server` as a second k3s node for real multi-node HA.

## Risks & open questions

- **Double control-plane during transition.** Two config systems (Ansible + Nix) coexist in one
  repo through Phase 1. Mitigation: clear ownership split (which host owns which service is
  explicit in `nix/hosts/*` vs `ansible/inventory/host_vars/*`), and the ingress/monitoring/backup
  planes stay single-sourced on the server.
- **sops-nix key path convention.** The existing setup uses per-host age keys at
  `~/.config/sops/age/keys.txt`; sops-nix defaults elsewhere (e.g. `/var/lib/sops-nix/key.txt`).
  Resolve the exact key path during Phase 0 so both tools read the same key. (Plan-time detail.)
- **AMD 780M transcoding.** VAAPI/AV1 is a Phase-3 media-relocation concern, not Phase 1; note it
  so the eventual media move exploits the iGPU rather than falling back to CPU.
- **CI cost.** `nix flake check` in GitHub Actions needs a Nix install + cache; keep it a single
  gated job to avoid ballooning CI time.

## Definition of done (per phase)

- **Phase 0:** NixOS installed; flake builds; sops-nix decrypts a secret into `/run/secrets`;
  `littlelink` reachable via existing ingress, green in Kuma, scraped by Prometheus, backed up by
  the node's Kopia; rollback proven.
- **Phase 1:** Rungs 1–3 migrated (Rung 4 optional); `mk-oci` helper stable; per-service files
  ~10 lines; CI runs `nix flake check` + Nix lints; Renovate bumps `flake.lock`.
- **Phase 2:** single-node k3s runs Rung 0→2 as manifests; cluster reachable through chosen
  ingress; a stateless service demonstrably **scales** (replicas + HPA) and does a **blue-green**
  cutover with rollback; own spec written and executed.
- **Phase 3:** decision made deliberately, with a storage plan if media relocates.
