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
  = mechanics, not HA). Datastore = embedded **etcd** from day one; **Flux** is the GitOps engine;
  secrets reach pods via the sops-nix bridge (see *Pre-implementation decisions*).
- **Phase 3 — Decision gate (deferred).** Convert `daniel-server` to NixOS and/or grow the k3s
  cluster; potentially relocate media/storage to make the K8 Plus primary. **HA reality:** k3s
  embedded etcd needs an odd quorum ≥3, so a *second* node is **not** HA (it doubles the failure
  surface) — genuine control-plane HA needs a **third** capable server (the Pi is too weak to be an
  etcd member). Real blue-green/scaling HA therefore depends on a future 3rd box, not on the server
  merely joining. Explicitly not committed now.

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
      disko.nix           # declarative btrfs layout (disko + nixos-anywhere provisioning)
      facter.json         # nixos-facter hardware capture (replaces hardware-configuration.nix)
    # (Phase 3) daniel-server/   ← additive, not a rewrite
  modules/
    common.nix            # ubuntu:1000/1000, TZ America/Chicago, ssh, nix settings, base firewall
    sops.nix              # sops-nix → ansible/vars/secrets.yml + host age key
    monitoring.nix        # node-exporter + cadvisor
    backup.nix            # kopia oci-container → same B2 repo
    services/<svc>.nix    # one per migrated service (mirrors ansible/roles/containers/<svc>)
  lib/
    mk-oci.nix            # macro analogue: traefik-route + kuma + healthcheck + podman defaults
kubernetes/               # (Phase 2) reserved now — Flux tree: apps/, infrastructure/, clusters/
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
  the `.sops.yaml` auto-encrypt already cover `vars/`; no new secret-scan gap. The node's age
  identity is derived from its **SSH host key** (`ssh-to-age`), not a copied personal key. Phase 2
  extends the *same* store to k8s via `sops.templates` → real `Secret` manifests (applied by
  `services.k3s.manifests`) — **not** Flux-SOPS / External-Secrets / sops-operator, each of which
  would add a second encrypted store. See *Pre-implementation decisions*.
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
- **Ingress — single *public* Traefik, file-provider routes.** New-node services get routes via
  Traefik's file-provider **directory** (the inode-trap fix already mounts a directory) pointing at
  `http://10.0.0.<k8plus>:<port>` — Authelia forward-auth, CrowdSec, and rate-limit@file all still
  apply centrally. NixOS `networking.firewall` restricts those ports to LAN/server only
  (declarative firewall = a clean Nix lesson). In Phase 2, k3s's bundled Traefik is disabled and
  the same public Traefik reaches pods via NodePort — **blue-green works with no in-cluster
  splitter**; only weighted canary (low value at homelab traffic) would add an internal in-cluster
  Traefik. So "single Traefik" stays true for the *public* edge throughout.

## Pre-implementation decisions (research-validated 2026-07-08)

Distilled from a four-domain research pass (NixOS host · k3s-on-NixOS · GitOps/secrets/CI ·
cross-cutting platform). Each item is irreversible or expensive to retrofit, so it is settled
before implementation. Tags: **[NOW]** must be right before first install/build · **[PAPER]**
decided now, built in the noted phase · **[DEFER]** reserved, not built.

### Host foundations
- **[NOW] Provisioning:** `disko` + `nixos-anywhere` + `nixos-facter` from the workstation. Disk
  layout is the most retrofit-hostile artifact (disko is destructive-only) — declaring it makes a
  reinstall one command.
- **[NOW] Root filesystem = btrfs** on a 1 GiB ESP + subvolumes (`@root`/`@nix`/`@persist`/`@home`/
  `@varlog`), `compress=zstd,noatime`; **zram** swap; **systemd-boot/UEFI** (keeps lanzaboote
  optional later). btrfs gives cheap learning-rollback snapshots without ZFS's ARC RAM tax on a
  32 GB box.
- **[NOW] No tmpfs impermanence** on this node — it's a stateful container/k3s box; the persist
  surface (podman/k3s state, PVs) is large and couples to the SSH-host-key/sops trap. btrfs
  snapshots cover "oops." Revisit in Phase 3.
- **[NOW] sops-nix key model:** node age identity derived from its **SSH host key** (`ssh-to-age`),
  added as a recipient to `ansible/.sops.yaml` → `sops updatekeys` (existing bootstrap flow).
  Pre-seed the host key via `nixos-anywhere --extra-files`. Never copy the operator's personal age
  key onto the box.
- **[NOW] AMD free bits on:** `hardware.enableRedistributableFirmware` + `cpu.amd.updateMicrocode`
  (via `nixos-hardware`). **[DEFER]** 780M VAAPI/AV1 transcode config until the media move.
- **Deploy (confirmed):** `nixos-rebuild --flake` → colmena; container backend = rootful
  `oci-containers` (podman). Rootless podman is buggy on 25.05 — use quadlet-nix later if wanted.

### Cluster (k3s, Phase 2)
- **[NOW-at-build] Datastore = embedded etcd (`clusterInit=true`) from day one.** On NixOS the
  datastore flag is bootstrap-only; etcd on NVMe is cheap, teaches snapshot/restore, and keeps
  multi-node additive.
- **[NOW-at-build] Pin pod/service CIDRs** in a shared module (defaults `10.42/16`+`10.43/16` are
  collision-free vs LAN `10.0.0.0/24` + wg `10.8.0.0/24`); reserve future subnets outside them.
  CIDRs can't change without a cluster rebuild.
- **[NOW] etcd snapshots → B2** on a schedule, and **the k3s server token in SOPS** (restore is
  impossible without it).
- **[PAPER] Storage = local-path** on a Kopia-scanned `./data` bind path (Kopia backs it up
  directly — no Velero, no hostPath limitation). Longhorn/CSI only at multi-node.
- **[PAPER] Progressive delivery = Argo Rollouts** (standalone; blue-green needs no traffic
  manager). metrics-server ships with k3s → **author CPU/mem resource requests on every manifest**
  so HPA is a no-op to enable.
- **[DEFER]** MetalLB, cert-manager, Velero, Cilium (flannel default unless NetworkPolicy/Hubble is
  an explicit goal — then choose Cilium *at install*, it's painful to retrofit).

### GitOps / secrets / CI
- **[PAPER] GitOps engine = Flux.** Pull-based (matches `gitops_deploy`), light footprint,
  monorepo-native. Reserve `kubernetes/` = `apps/` + `infrastructure/` + `clusters/`; Kustomize for
  own apps, Helm (`HelmRelease`) for third-party charts.
- **[NOW — write it down] Secrets bridge = sops-nix reading the existing `secrets.yml`** — Phase 1
  via `environmentFiles`, Phase 2 via `sops.templates` → real `Secret` manifests applied by
  `services.k3s.manifests`. **NOT** Flux-SOPS / External-Secrets / sops-operator (each = a second
  encrypted store; all three rejected options quietly pull toward that).
- **[PAPER] Renovate:** one config; flip on the `nix` manager when the flake lands (keep the
  `github:NixOS/nixpkgs/...` input form so `flake.lock` refreshes); extend patterns for
  `kubernetes/` + `flux` at Phase 2.
- **[PAPER] CI/prek:** `nix flake check` + alejandra/statix/deadnix at Phase 1 (Nix-in-Actions via
  Determinate/FlakeHub-cache or Cachix or plain GH-Actions cache — **magic-nix-cache's free tier
  ended Feb 2025**); kubeconform/`flux-schema` on `kubernetes/**` path-filters at Phase 2.

### Platform / physical
- **[NOW] Kopia identity before host #2 connects:** distinct `user@hostname`; stand up a **Kopia
  repository-server + ACLs on daniel-server** so the node can't delete the server's snapshots. One
  B2 repo (keeps dedup).
- **[NOW — at mkfs, future disk] Media pool = XFS `reflink=1`** (or btrfs), **single mount** for
  downloads+library — reflink can't be enabled post-`mkfs`. It's a separate additive OCuLink disk,
  decoupled from the root FS; the Phase-3 move becomes a data copy, not a reformat.
- **[NOW — BIOS/physical] Before install:** confirm UPS free outlet + VA headroom → add node as a
  **NUT netclient**; enable **Wake-on-LAN + restore-on-AC-power**; **disable i226-V ASPM** (known
  2.5GbE-drop bug); and since the box has **no IPMI/vPro**, wire a **firewall auto-revert timer**
  before the first remote `nixos-rebuild` (a bad firewall push on a headless box = physical trip).
- **[NOW — convention] Prometheus:** stamp `host="daniel-<node>"` on every job so monitoring is
  node-aware from metric #1 (the instance-blind gotcha).
- **[PAPER] Networking:** NIC1 on the flat LAN now; **reserve NIC2 + a `10.0.10.0/24` storage
  subnet** for a future dedicated server↔node link. No LACP, no overlay mesh (Headscale if ever),
  no VLANs yet.
- **[NOW — convention] Node identity:** name it for its role, not the SKU (e.g. `daniel-node`),
  since Phase 3 may repurpose it as primary; static reservation + a `.lan` A-record in Pi-hole
  (extends the existing `*.local.daniel-hunter.com` split-horizon scheme — no new internal DNS
  server needed). The flake host attribute (`k8plus` in examples above) is just an internal label.

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
ConfigMaps/Secrets (via the sops-nix → `Secret`-manifest bridge), and Ingress on familiar
workloads. Cluster on **embedded etcd**, GitOps by **Flux**, progressive delivery by **Argo
Rollouts** (see *Pre-implementation decisions*).

**Blue-green + scaling are the headline Phase-2 lessons**, exercised on the stateless subset:
- **Scaling:** replicas + a Service load-balancer; then autoscaling (HPA + metrics-server) on
  `littlelink`/`bento-pdf`. Single node caps scale at the box's resources — that limit is accepted.
- **Blue-green:** start with k8s rolling updates (native), then a true blue-green/canary cut with
  **Argo Rollouts** (or weighted Traefik-Gateway routing) — hold two versions, shift traffic,
  roll back by flipping. Stateful singletons keep NixOS-generation-style deploys, not blue-green.

Ingress: k3s's bundled Traefik is disabled; the existing server Traefik reaches pods via NodePort
(blue-green needs no in-cluster splitter; only weighted canary would add an internal Traefik). This
phase gets its own spec before implementation.

## Phase 3 — Decision gate (deferred)

Revisited only after the K8 Plus runs well standalone. Options, non-binding:
- Convert `daniel-server` to NixOS: additive — add `nix/hosts/daniel-server/`, reuse the
  `services/<svc>.nix` modules already written in Phase 1.
- Make the K8 Plus primary + relocate media: requires a storage plan (OCuLink NVMe or large
  internal SSD), moving the entire linked media set to one filesystem, and re-pointing Kopia.
- Grow the k3s control plane: a *second* node is **not** HA (embedded etcd needs an odd quorum
  ≥3) — genuine HA needs a **third** capable server; the Pi cannot be an etcd member.

## Risks & open questions

- **Double control-plane during transition.** Two config systems (Ansible + Nix) coexist in one
  repo through Phase 1. Mitigation: clear ownership split (which host owns which service is
  explicit in `nix/hosts/*` vs `ansible/inventory/host_vars/*`), and the ingress/monitoring/backup
  planes stay single-sourced on the server.
- **sops-nix key model (resolved).** Derive the node's age identity from its **SSH host key**
  (`ssh-to-age`) — root-readable at activation, matches the existing bootstrap flow, keeps the
  operator key off the box. Pre-seed the SSH host key via `nixos-anywhere --extra-files`, else first
  activation fails to decrypt.
- **AMD 780M transcoding.** VAAPI/AV1 is a Phase-3 media-relocation concern, not Phase 1; note it
  so the eventual media move exploits the iGPU rather than falling back to CPU.
- **CI cost / cache EOL.** `nix flake check` in Actions needs a Nix install + cache; **the free
  `magic-nix-cache` tier ended Feb 2025** — use Determinate/FlakeHub-cache, Cachix, or the plain
  GH-Actions cache. Keep it one gated job.
- **"2 nodes = HA" is false.** k3s embedded etcd needs an odd quorum ≥3; real control-plane HA
  needs a 3rd capable box (Pi excluded). Phase-3 HA aspirations must budget for that, or accept a
  single control plane.
- **No IPMI/vPro + declarative firewall = lockout risk.** A bad `networking.firewall` push on a
  headless, up-but-unreachable box can't be fixed by WoL. Mitigate with a firewall auto-revert
  timer + BIOS restore-on-AC-power before the first remote rebuild (JetKVM is the deferred
  out-of-band answer).
- **i226-V 2.5GbE ASPM bug.** The dual NICs are Intel i226-V; disable ASPM at build to avoid the
  known intermittent-link drop.
- **etcd DR token.** k3s snapshot restore is impossible without the original server token — store
  it in SOPS alongside the host keys.

## Definition of done (per phase)

- **Phase 0:** node provisioned declaratively (disko + nixos-anywhere, btrfs root); flake builds;
  node age key onboarded (SSH-derived, in `.sops.yaml`) and sops-nix decrypts a secret into
  `/run/secrets`; NUT netclient + WoL/AC-restore + firewall auto-revert path in place;
  `littlelink` reachable via existing ingress, green in Kuma, scraped by Prometheus (with the
  `host=` label), backed up by the node's Kopia (repo-server identity set); rollback proven.
- **Phase 1:** Rungs 1–3 migrated (Rung 4 optional); `mk-oci` helper stable; per-service files
  ~10 lines; CI runs `nix flake check` + Nix lints; Renovate bumps `flake.lock`.
- **Phase 2:** single-node k3s runs Rung 0→2 as manifests; cluster reachable through chosen
  ingress; a stateless service demonstrably **scales** (replicas + HPA) and does a **blue-green**
  cutover with rollback; own spec written and executed.
- **Phase 3:** decision made deliberately, with a storage plan if media relocates.
