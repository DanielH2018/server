# Zero-downtime deploys — design

**Date:** 2026-08-16
**Status:** design approved, spec pending review
**Scope:** approaches B + C from the 2026-08-16 triage, plus the A-items B builds on.

## Problem

Every homelab-authored Deployment is `replicas: 1`. Of 55, **41 use `strategy: Recreate`** —
the old pod stops before the new one starts, so each deploy of those services has a hard
downtime gap (estimated 15–45s from probe configuration; not measured). The remaining 14
have no `strategy:` block, so they default to `RollingUpdate`, and on a single replica
`maxSurge` rounds up to 1 — a second pod surges and there is no gap.

The `Recreate` choices are deliberate and individually documented in their templates. The
goal here is not to overturn them wholesale, but to convert every workload where rolling is
genuinely safe, and to reduce the gap for the ones where it isn't.

## Triage: why the 41 are on Recreate

Classified from each role's image, mount paths, and the rationale comments already in the
templates. This is inference, not per-app doc confirmation — sufficient to justify leaving a
service alone, **not** sufficient to justify converting one (see *Conversion gate* below).

| Blocker class | Count | Services |
|---|---|---|
| sqlite / embedded DB / local config store | 16 | sonarr, radarr, bazarr, prowlarr, jellyfin, freshrss, healthchecks, speedtest, uptime-kuma, n8n, home-assistant, authelia, crowdsec, grafana, scrutiny-web, karakeep |
| single-writer TSDB / index | 6 | prometheus, loki, loki-homelab, tempo, scrutiny-influxdb, karakeep-meilisearch |
| single-writer datastore / world save | 5 | livesync (couchdb), mosquitto, valheim, terraria, tdarr |
| node-exclusive hardware or netns | 5 | nut (USB), zigbee2mqtt (radio accepts one client), wg-easy, qbittorrent (VPN killswitch netns), registry (hostPort) |
| in-process state | 5 | monitor-bridge, autofix-bridge, janitorr, valheim-stats, terraria-stats |
| ingress / DNS topology | 2 | traefik, pihole |
| workspace | 1 | code-server |
| stateless — convertible today | 1 | prowlarr-flaresolverr |

Rows sum to 41, verified against the rendered manifests rather than counted by hand. The
`claude-otel` role contributes four entries (grafana, prometheus, loki, tempo), `karakeep`
two (karakeep, meilisearch — its `karakeep-chrome` and `karakeep-time-tagger` deployments
already roll), `scrutiny` two (influxdb, web), and `prowlarr` two (prowlarr, flaresolverr).

Two lines of attack were checked and closed:

- **No deployment's only PVC is a cache.** There is no free "drop the PVC" conversion. The
  cache mounts that exist (`freshrss` `/config/www/freshrss/data/cache`, `n8n`
  `/home/node/.n8n/.cache`, `karakeep-tagger` `.next/cache`) sit alongside a `/config` or
  `/data` PVC that still blocks.
- **No two Recreate deployments share a claim.** Co-location as a conversion mechanism has
  essentially no candidates.

### Unverified assumption

Whether Longhorn v1.12 permits two pods on the **same node** to mount one RWO volume could
not be confirmed: the docs consulted do not state it, and it cannot be tested from this
session (the `homelab-readonly` service account has `get list watch` only; Ansible is the
only write path to the cluster). All 21 Longhorn volumes report `accessMode: rwo,
migratable: false`.

This assumption gates only the co-location mechanism, whose candidate set is ~1 service, so
nothing in this design depends on it. **Do not build on it without testing it first.**

## Three mechanisms, not one

"Zero-downtime" is reachable three different ways, and rolling replicas is only the first.
Assigning each workload to the right mechanism is what makes this tractable:

1. **RollingUpdate + co-located replicas** — requires app-level multi-writer tolerance.
   Candidate set here: `prowlarr-flaresolverr`.
2. **Two independent instances with their own state** — separate PVCs, separate state,
   client-side or VIP-level failover. Correct where the state is *derived* rather than
   authoritative. Candidate: **Pi-hole** (the gravity DB is regenerated, not authoritative).
3. **Move authoritative state out of the pod** — a shared Postgres, so the pod becomes
   stateless enough to run two replicas. This is approach C, and it is the only mechanism
   that moves the long tail.

## Approach C — shared Postgres

### Postgres must itself roll

An ordinary single Postgres Deployment would relocate the downtime rather than remove it.
**CloudNativePG (CNPG)** resolves this: it upgrades replicas first, then switches over to a
replica already running the target image. Its docs describe `primaryUpdateMethod: switchover`
as ensuring "the promoted instance already runs the target image version of the container."

This requires **≥2 instances** — one instance has nothing to switch over to. The CNPG docs
do not state single-instance behaviour explicitly; treat 2 as the floor. Each instance gets
its own RWO PVC, so there is no shared-volume problem. With two cluster nodes, that is one
instance per node, and **the cluster loses its redundancy entirely whenever either node is
down for maintenance** — an accepted limitation, not a solved one.

### Redis is not optional

Authelia's default session provider is in-memory. Its documentation states: *"By default
Authelia uses an in-memory provider. Not configuring redis leaves Authelia stateful."* Two
Authelia replicas without a shared session store means a user's session breaks depending on
which pod the request lands on. n8n's multi-instance mode (queue mode) also requires Redis —
**unconfirmed**, the n8n doc page returned 404; confirm before scoping slice 9.

So C introduces **two** new platform components before any app converts.

### Postgres removes the volume blocker, not the singleton blocker

A rolling update overlaps two pods for roughly ten seconds. That is only safe if the app is
built for concurrent instances. Postgres support and multi-instance safety are different
properties, and conflating them is the main way this design could cause damage:

| Service | Postgres | Multi-instance safe | Outcome |
|---|---|---|---|
| grafana | yes | **confirmed** — shared DB, token auth, no session affinity; unified alerting needs `ha_peers` or notifications duplicate | rolling, 2 replicas |
| authelia | yes | yes, **with Redis** for sessions | rolling, 2 replicas |
| healthchecks | yes | likely (Django, DB-backed sessions) — confirm | rolling, 2 replicas |
| freshrss | yes | **amber** — PHP, sessions are almost certainly file-based; two replicas would randomly log users out | Postgres yes; rolling only if a shared session handler checks out |
| sonarr / radarr / prowlarr | yes (v4/v5) | **no** — both instances would fire scheduled RSS syncs and import jobs during the overlap | Postgres for durability; **stays Recreate** |
| n8n | yes | needs queue mode + Redis — confirm | pending |
| karakeep | unknown | unknown — confirm | pending |
| home-assistant recorder | yes | no — singleton with device connections and a large `/config` | out of scope; buys nothing |

Expected outcome: **41 Recreate → ~35**, with 4–6 services reaching true zero-downtime, at
the cost of two new platform components and 7–9 data migrations. This ratio is poor on
paper and was accepted deliberately; slice 8 in particular carries migration risk for a
durability win with no downtime payoff.

### Backups

Longhorn volume backups cover the CNPG PVCs, as with every other volume. Longhorn snapshots
of a live Postgres are crash-consistent and Postgres replays WAL on restore, so this is a
valid recovery path — but it restores the whole cluster, not one app's database.

Pair it with a **nightly `pg_dump` CronJob** writing into an already-backed-up volume: one
backup system to operate, no WAL archiving, and a per-app restore path. CNPG's native WAL
archiving was considered and rejected — it is a second backup system, and the B2 account is
already at a cap ceiling that has been breached repeatedly.

Note the existing `longhorn-nobackup` storage class and `k3s_longhorn_nobackup_volumes`
mechanism if the CNPG PVCs should be excluded from Longhorn backups once `pg_dump` covers
them.

## Approach B — shrink the gap on the holdouts

For the ~35 workloads that stay on `Recreate`, the gap can still be cut:

- **`minReadySeconds`** where a pod reports ready before it is genuinely serving.
- **`terminationGracePeriodSeconds`** tuned down where the app shuts down fast — the default
  30s is spent entirely inside the downtime window on a `Recreate` rollout.
- **Image pre-pull**, so a new tag is not pulled *inside* the gap. Pinned digests are already
  the convention; the gap case is a changed tag.

This does not reach zero. It is expected to move an estimated 15–45s window to roughly
5–15s across the holdouts — an estimate to be replaced with measurement in slice 1.

## Approach A items (folded into B)

- **`prowlarr-flaresolverr` → RollingUpdate.** Stateless Chrome captcha solver; its PVC
  needs confirming as content-free, then dropping.
- **Pi-hole redundancy.** A second instance with its own PVC and its own VIP. Today LAN DNS
  is a single `dns_k8s_vip` (10.0.0.243), so a Pi-hole rollout takes all LAN DNS down.
  Client distribution across two VIPs is a router/DHCP decision that may fall outside
  Ansible — resolve during the slice.
- **Enforcement lint.** A pytest guard asserting (a) every `strategy: Recreate` carries a
  rationale comment, and (b) every rolling Deployment behind a Service has a
  `readinessProbe`. This turns 41 scattered comments into a maintained, machine-checked
  policy, and stops the next service added from silently getting it wrong.

## Conversion gate

Every service this design proposes to **convert** must have its write model and
multi-instance behaviour confirmed against the app's own documentation or configuration
before the change is made. The triage table above is inference from image names and mount
paths; that is adequate to justify a holdout and inadequate to justify a change that could
corrupt a database.

Every Postgres migration needs a rehearsed rollback: dump sqlite → import → verify → **keep
the sqlite volume until the migration is proven**.

## Slices

Sequenced so each is independently exercisable, and so the platform cost is paid only after
the cheap wins have landed.

| # | Slice | Exercisable by | New platform |
|---|---|---|---|
| 1 | B: gap-shrinking across the holdouts + flaresolverr → rolling + enforcement pytest | time a deploy before/after | — |
| 2 | Pi-hole redundancy (2nd instance, own PVC + VIP) | `dig` loop while one instance rolls | — |
| 3 | CNPG operator + 2-instance cluster + nightly `pg_dump` | scratch DB; kill primary, watch switchover | CNPG |
| 4 | **grafana** → Postgres, 2 replicas, RollingUpdate | request loop across a rollout, zero failures | — |
| 5 | Redis + **authelia** → Postgres, 2 replicas | request loop + session survives the rollout | Redis |
| 6 | **healthchecks** → Postgres, 2 replicas | request loop | — |
| 7 | **freshrss** → Postgres (rolling only if sessions resolve) | request loop + login persists | — |
| 8 | **sonarr / radarr / prowlarr** → Postgres, stay Recreate | app starts, library intact | — |
| 9 | **n8n / karakeep** → pending confirmation | request loop | — |

Grafana leads the conversions rather than Authelia deliberately: it is confirmed
multi-instance-safe, needs no Redis, and its blast radius on failure is one dashboard.
Authelia's blast radius is every service behind SSO, so it goes second, once the pattern is
proven.

Each new service must be positioned in `containers_list` in
`ansible/inventory/host_vars/daniel-box.yml` after `traefik` (CRDs) and after `authelia`
where the route uses that middleware — the play runs in list order with no toposort.

## Acceptance test

The same shape for every conversion: a request loop against the service while
`kubectl rollout restart` fires, asserting zero failed requests. For Pi-hole, a `dig` loop
asserting zero failed resolutions.

This is the only evidence that separates "configured `RollingUpdate`" from "actually
zero-downtime". No slice is complete on the strength of its manifest.

## Holdouts — deliberate, with reasons

These stay `Recreate` and the enforcement lint should expect them to:

- **traefik** — `externalTrafficPolicy: Local` plus a MetalLB VIP. Two pods pinned to the
  announcing node still contend for the same announcement path, and this repo has a
  recorded history of ETP-Local VIP blackouts when the announcer must forward traffic. A
  second Traefik pod is a landmine, not an improvement.
- **Single-writer TSDBs** (prometheus, loki, tempo, influxdb, meilisearch) — a second
  instance cannot acquire the data-directory lock.
- **Node-exclusive resources** (nut's USB, zigbee2mqtt's radio, wg-easy, qbittorrent's VPN
  netns, registry's hostPort).
- **Game worlds** (valheim, terraria) and **tdarr** — two writers is worse than a gap.
- **In-process state** (monitor-bridge, autofix-bridge, janitorr, the stats sidecars) —
  grace-cycle and hysteresis streaks do not survive being split across two pods.
- **karakeep** — sqlite on its data PVC. Its `karakeep-chrome` and `karakeep-time-tagger`
  deployments already roll and are not holdouts.
- **home-assistant**, **code-server**, **livesync**, **mosquitto**, **crowdsec**,
  **uptime-kuma**, **speedtest**, **jellyfin**, **bazarr**, **karakeep-meilisearch**.

## Open questions

1. n8n queue-mode requirements — the doc page 404'd. Gates slice 9.
2. karakeep's data model and multi-instance behaviour. Gates slice 9.
3. FreshRSS session storage — determines whether slice 7 delivers rolling or only Postgres.
4. Pi-hole client distribution across two VIPs: DHCP option, router config, or both — may
   be outside Ansible's reach.
5. Whether the CNPG PVCs should be excluded from Longhorn backups once `pg_dump` covers them.
