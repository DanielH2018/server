# Zero-downtime deploys — design

**Date:** 2026-08-16
**Status:** slice 1 shipped; scope reduced 2026-08-16 to three remaining items

**Scope reduction.** This spec originally proposed approaches B and C across nine slices. Slice
1 shipped and its measurement withdrew approach B's fleet-wide tuning; the open questions then
cut two more slices. What remains is **Pi-hole redundancy** plus two pieces of hygiene. The
Postgres work is deferred to a separate programme justified by data durability rather than
downtime — see *Slices* for why. The sections below are kept as the reasoning that led here,
not as work in flight.

**Branch status (worktree-zero-downtime-deploys-spec):** slice 1 shipped and is deployed.
`prowlarr-flaresolverr` runs `RollingUpdate` + `emptyDir` on the live cluster, its PVC is
retired, and the rollout was measured — see *Measured results*. The triage table and counts
below are the historical record from the 2026-08-16 triage and are deliberately left unchanged;
they say 41/14/21 where the live figures are now **40 `Recreate` / 15 rolling of 55** and **20**
Longhorn RWO volumes.

**Original scope:** approaches B + C from the 2026-08-16 triage, plus the A-items B builds on.
Superseded by the scope reduction above; retained because the reasoning below argues from it.

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

**Measured, and withdrawn.** This section proposed the tuning above on the strength of an
estimated 15–45s window. The measurement in [`zero-downtime-baseline.md`](zero-downtime-baseline.md)
does not support it: median start→ready across the fleet is 11s, p90 is 31s, and
`terminationGracePeriodSeconds` only contributes at all when an app ignores SIGTERM — which
nothing here is shown to do. `minReadySeconds` runs the wrong way; it delays a rollout being
called complete rather than shrinking a gap.

The real outliers are radarr (310s) and sonarr (250s), an order of magnitude past the estimate
and caused by application startup that no Kubernetes setting touches.

So approach B's fleet-wide tuning is dropped. The baseline's recommendation stands in its place:
leave the ~35 typical holdouts alone, treat radarr and sonarr as an application question if their
4–5 minute gaps matter, and add `readinessProbe`s to the 14 workloads with none — a correctness
fix, and a prerequisite for ever converting them.

> **That "14 with none" figure is wrong, and so is the audit method behind it.** Executing the
> probe work found the cause: both `scripts/startup_baseline.py` and the audit that produced
> plan 3 counted containers with no `readinessProbe` key, and **a `startupProbe` gates readiness
> too** — a container is not Ready until its startup probe succeeds, so a Service does not
> publish it. Of the three workloads the audit flagged as behind a Service with no gate, two
> already had one: `terraria` (`deployment.yaml.j2:59`, exec `grep :1E61 /proc/net/tcp`,
> `failureThreshold: 30`) and `valheim` (`:102`, exec `grep :0998 /proc/net/udp`,
> `failureThreshold: 60` — 15 minutes, sized for a first boot that pulls ~1.8G through SteamCMD).
> Only `nut` genuinely lacked a gate. Worse, adding the audited probe to terraria would have been
> actively harmful: that template documents at `:54-56` that the game logs every accepted socket,
> so a `tcpSocket` connect probe spams the game console. **Any future probe-coverage audit in this
> repo must treat `readinessProbe` OR `startupProbe` as coverage.** `ansible/tests/
> test_readiness_coverage.py` currently records the two startup-gated workloads as allowlisted
> reasons; teaching it to recognise `startupProbe` directly is the cleaner fix and is the one
> recommended follow-up from this slice.

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

**Waiver recorded — flaresolverr (slice 1):** the write-model half of the gate was confirmed
(`/config` is a regenerable browser profile cache); the multi-instance half was not confirmed
against FlareSolverr's own documentation — see the strategy comment in
`deployment-flaresolverr.yaml.j2` for the risk assessment and why it's an inference, not a
citation. Recorded here because this gate also protects the database migrations in later
slices, and an unrecorded waiver on the first conversion sets the wrong precedent.

## Slices

**Scope reduced 2026-08-16, after slice 1 measured.** The original nine slices are below with
their outcome. Three remain.

| # | Slice | Status |
|---|---|---|
| 1 | Measurement probe, strategy guard, flaresolverr → rolling | **done** — PR #233 |
| 1b | Baseline the holdouts | **done** — and it withdrew approach B's tuning |
| 2 | **Pi-hole redundancy** (2nd instance, own PVC + VIP) | **keep — highest value remaining** |
| 3–6, 8 | CNPG + grafana / authelia / healthchecks / \*arr migrations | **deferred to its own programme** (below) |
| 7 | freshrss → Postgres, rolling | **cut** — sessions are file-based |
| 9 | n8n / karakeep | **cut** — see Resolved questions |

Plus two pieces of hygiene slice 1 surfaced, both cheap:

| Item | Why |
|---|---|
| Widen the 3.12 syntax guard to every `scripts/*.py` with a `python3` shebang | `ruff format` stripping `except (A, B)` parentheses bit **three times** on one branch. Three recurrences is the threshold for an executable check. |
| Add `readinessProbe`s to the 14 workloads with none | Their pods are Ready the instant the container starts. Harmless under `Recreate`; a correctness bug the moment any is converted. |

### Why CNPG is deferred rather than dropped

The Postgres work was justified in this spec by zero-downtime. That justification did not
survive the slice-1 measurement and the cuts above: with freshrss and n8n/karakeep gone, it
converts **three** services — grafana, authelia, healthchecks — at the cost of two new platform
components (CNPG, Redis) and several data migrations. That is not a highest-value item.

**The durability argument is separate and still stands.** sqlite on a single-replica RWO volume
is the likeliest corruption risk in this homelab, and moving the \*arr databases off it was
wanted for its own sake. That is a data-integrity programme, not a zero-downtime one, and it
should be specced on those terms — with its own justification, its own risk assessment, and
rolling replicas as an incidental bonus for the three services that can take them.

Conflating the two is what made this spec propose two platform components for a benefit that
measurement then shrank. Kept here so the rationale is not lost.

### Retained detail for the deferred work

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

Resolved 2026-08-16:

1. **n8n queue-mode requirements — still unanswered, and no longer worth answering.** Every
   docs URL tried returned 404. Queue mode means Redis plus separate main and worker
   processes; n8n here is a single-user automation tool whose gap delays a trigger rather than
   losing one. Cut on effort-to-payoff rather than blocked on research.
2. **karakeep — answered from the repo, not its docs.** It is two Deployments, and
   `karakeep-meilisearch` holds an exclusive LMDB lock, so it stays `Recreate` regardless of
   what happens to karakeep's own database. Converting half a service leaves the gap. Cut.
3. **FreshRSS session storage — file-based.** The role carries no PHP session configuration, so
   it runs the image default: sessions on disk in the data directory. Two replicas would log
   users out at random. Rolling is cut; Postgres for durability belongs to the deferred
   durability programme.
4. **Pi-hole client distribution — two VIPs, one per node, both handed to clients by router
   DHCP.** The tidier-looking option of one VIP with two backing pods does not work here: the
   Service is `externalTrafficPolicy: Local`, so a pod on the non-announcing node receives
   nothing, and putting both on one node dies with that node. Accepted caveat: failover is
   client-side, so a client whose primary is down stalls until its resolver times out. Still
   far better than every client failing for the length of a rollout.
5. **CNPG PVCs and Longhorn backups — exclude them; use `pg_dump`.** The `longhorn-nobackup`
   storage class exists for this. Volume backups of a live Postgres are crash-consistent but
   restore the whole cluster rather than one app's database, and this B2 account has a
   documented history of breaching its cap. Carried into the deferred durability programme.

Still open:

6. Whether the FlareSolverr multi-instance waiver (see *Conversion gate*) should be closed with
   a documented confirmation. Low priority — the risk is small and the rollout measured clean.

## Measured results

| Slice | Service | Date | Samples | Zero-ready | Longest gap | Signal |
|---|---|---|---|---|---|---|
| 1 | flaresolverr | 2026-08-16 | 1029 | 0 | 0.00s | ready Service endpoints |
| 2 | pihole (DNS) | 2026-08-16 | 881 | 0 | 0.00s | real UDP DNS queries from daniel-server |

**Slice 2 is the stronger of the two results**, and for the reason slice 1 lacked: the signal is
an actual request loop. `scripts/dns_witness.py` sent a real UDP A query for
`pi.hole` at the `pihole-dns` VIP (10.0.0.243) every 0.25s **from daniel-server**, counting a
sample as OK only on a well-formed response with rcode 0 and at least one answer. So this
measures what a LAN client experiences, from a host that is not the one being deployed — not
endpoint bookkeeping on the deploying node. 881 queries spanned both deploy phases end to end;
zero failed.

What the 881 samples actually covered:

- **Phase 1** — the deploy that creates `pihole-2`. This one also *changed instance 1's pod
  template* (it gained the `instance:` label), so `kubectl apply` Recreate-cycled instance 1
  before `roll_one` ever ran — the template-change limitation documented at
  `roles/k8s/pihole/tasks/main.yml:24-33`, hit on the very deploy that introduces the label.
  It still cost no downtime: the terminating pod kept answering through its 30s grace period
  while `pihole-2` reached ready in ~25s.
- **The sibling-ready gate earned its keep here.** `roll_one` held instance 1's restart for three
  retries (~30s) with `Verify the sibling instance is ready before restarting pihole` until
  `pihole-2` — provisioning a fresh Longhorn PVC — was serving DNS. Without it, instance 1 would
  have been restarted into a window where the only other instance had no endpoints. That is the
  C2 finding, and this deploy is the exact scenario it was written for.
- **C1's fix confirmed by the change record**, not by inference: the gravity reconcile reported
  `ok => (item=pihole)` and `changed => (item=pihole-2)`. The two writes landed on two different
  pods. Before the `instance:` label, both would have resolved through the shared
  `spec.selector` to instance 1 and `pihole-2` would have served DNS with an empty gravity.db.
- **Phase 2** — an unchanged rerun. `roll_one` was correctly skipped by its
  `manifests_render is changed or …` gate and neither pod restarted (ages carried forward from
  phase 1). The only `changed` was `kubectl apply` itself, which always reports changed. This is
  the idempotence property the gate exists for: a full-fleet deploy must not Recreate-cycle both
  Pi-holes and their Longhorn volumes every run.

**The failover was real, not vacuous.** For roughly 45s — instance 1's 30s termination grace
plus its replacement's startup — `pihole-2` was the *only* ready endpoint on `pihole-dns`. On a
0.25s sampling interval that is on the order of 180 of the 881 queries answered by the new
instance alone. The zero is a measured handover, not an artefact of nothing having moved.

**"Redundancy" here means deploy-scoped, and only that.** Both instances are pinned to
`daniel-box` by `nodeSelector`, so losing that node still takes all LAN DNS with it. The pin is
forced and correct — MetalLB's L2Advertisement is pinned to `daniel-box` and the Service is
`externalTrafficPolicy: Local`, so a pod on the other node would be announced from a node that
cannot reach it. Stated explicitly because the word "redundancy" invites the stronger reading.

**A silently-dead instance 2 is already detected — no new monitoring needed.** The obvious worry
about two pods behind one Service is that losing one is invisible: DNS keeps answering from the
survivor, and you are back to a single instance with no signal until the next deploy takes LAN
DNS down. That is covered. `monitor-bridge`'s `k8s_workloads` check queries
`kube_deployment_status_replicas_unavailable > 0` with **no deployment-name filter**
(`roles/k8s/monitor-bridge/files/check.py:2426`, registered in `CHECKS` at `:2677`), and each
instance is its own `replicas: 1` Deployment with readiness and liveness probes — so a dead
`pihole-2` shows up as one unavailable replica regardless of what the Service is still serving.
A crashlooper that flickers through readiness is caught by the same check's restart arm. The
coverage is incidental rather than Pi-hole-specific: nothing in the repo monitors Pi-hole by
name beyond DNS resolution.

**There is one VIP, not two.** The original sketch imagined a second `dns_k8s_vip` for the
router's DHCP to hand out. The implementation did not need it: both instances carry `app: pihole`
and sit behind the single `pihole-dns` LoadBalancer (10.0.0.243), which kube-proxy already
load-balances across both endpoints. No router or DHCP change is required, and none should be
made — there is no second address to distribute.

**What slice 2 did NOT exercise.** The both-instances-roll path. On this deploy instance 2 was
newly created, so its `roll_one` iteration had nothing to restart and was skipped; only instance
1 actually rolled. The sequenced two-instance roll first runs on the next ConfigMap/Secret-only
change (a blocklist edit or a secret rotation) — which is also the only class of change this
redundancy protects, per the limitation above. Stated rather than implied, because a reader
could otherwise take the 0-gap row as proof of a sequencing behaviour that has not yet run.

**Read this before quoting the result.** It is weaker than the acceptance test this spec
prescribes, in two specific ways, and neither is a formality:

1. **It is not a request loop.** `scripts/measure_rollout_gap.py` polls HTTP, and flaresolverr
   cannot be polled that way: its NetworkPolicy refuses node-originated traffic (verified —
   `curl` to the ClusterIP from daniel-box fails), and a `kubectl port-forward` dies with the
   pod it attached to, which would fabricate a gap at exactly the moment being measured. So the
   signal is the count of *ready Service endpoints*, sampled every ~0.27s across a real rollout.
   That is the property `maxUnavailable: 0` actually promises, and it has no measurement
   artefact — but it proves a backend was routable, not that a request succeeded.
2. **The two-pod overlap was never directly observed.** Peak ready count was 1, not 2, so the
   EndpointSlice most likely swapped the old address for the new within a single update rather
   than briefly listing both. The result is consistent with a seamless handover and rules out a
   window with no backend at all; it does not demonstrate that an in-flight request survived
   termination.

The rollout was real and fell inside the sampling window: the deploy that triggered it changed
the `emptyDir` `sizeLimit`, `Roll the extra deployments … => (item=flaresolverr)` fired, and the
pod came back on a new ReplicaSet hash.

**Reproducing it.** The row above was originally produced by a throwaway script, because
`measure_rollout_gap.py` had no way to measure a workload a request loop cannot reach. That gap
is now closed — the mode is committed, and the same measurement is:

```bash
uv run python scripts/measure_rollout_gap.py --endpoints flaresolverr --seconds 280 --interval 0.25
# then, in another terminal, a deploy that actually changes a rendered manifest:
./scripts/deploy.sh --tags prowlarr
```

The mode reports peak ready backends alongside the gap count, and says plainly when it never
observed two at once — so a future reader gets the caveat from the tool rather than having to
remember it. It exits non-zero when it never saw a ready backend at all, so a mistyped service
name fails rather than reading as a clean pass.

**A deploy only measures something if it changes something.** The rollout is triggered by
`manifests_render is changed`; a deploy that re-applies identical manifests performs no rollout,
and polling across it yields a meaningless PASS against a stable service. That happened once
while producing this row. Check the pod's age or ReplicaSet hash afterwards to confirm a rollout
actually occurred before recording any result.
