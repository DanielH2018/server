# Slice-7 Phase D.1 — Uptime Kuma + AutoKuma to the cluster

Planned 2026-08-10, from the Phase D coupling inventory (130 `kuma()` call sites, 6
Docker-DNS push consumers, 8 `--resolve` pinners, 2 label-managed notifications). Decisions
KD1–KD7 below; execution steps at the end. Parent: `slice-7-drain-and-join.md` (D3/D4).

## What the inventory established

- **~27 docker-type monitors** render on daniel-server today (shrinking as Phase B/C
  retires land — code-server's and docker-proxy-codeserver's died tonight). Every one
  watches a container that is still Docker when Kuma moves, so all of them need an answer.
- **docker-proxy publishes no host port** (deliberate, Security M1) — cluster-side AutoKuma
  cannot reach `tcp://docker-proxy:2375` without reopening the M1 secret-enumeration
  decision.
- **Kuma's SQLite DB is not backed up** (`kopiaignore.j2` excludes it, by design). The seed
  is one-shot-correct or monitor history is lost.
- **`kuma_docker_host: 1` is a numeric FK into Kuma's own DB** pointing at
  `tcp://docker-proxy:2375` — a seeded PVC carries a URL the cluster can't resolve.
- Both notifications (discord — every monitor; email — the one Discord-Delivery monitor)
  are **label-provisioned by AutoKuma**, an experimental feature, and
  `notification_name_list` emission is conditional on `kuma_notification_id` being in
  scope — if it isn't where monitors render, everything gets created and nothing pages.
- Push consumers split cleanly: 6 Docker-DNS sites (`http://uptime-kuma:3001` — per-role
  env flips), 12 routed sites (`https://uptime-kuma.local.<domain>` pinned by
  `--resolve` to `hostvars[monitoring_controller_host].server_ip` — one variable flip
  covers 8 of them; 4 hard-code the hostname and follow it through the bridge).

## Decisions

### KD1 — Monitor source: ONE static-monitors file set, rendered from inventory

AutoKuma runs docker-less (`AUTOKUMA__DOCKER__ENABLED=false`,
`AUTOKUMA__STATIC_MONITORS=/monitors` from a ConfigMap). All monitor declarations move into
one Ansible-rendered template in the k8s/uptime-kuma role — the 19 hand-declared k8s/bridge
monitors and the cron-push labels move verbatim; the docker-type ones convert per KD2. Not
the Kubernetes-CRD source: our monitors are inventory-derived, and a rendered file keeps the
single source of truth, the render-validation hook, and the CI tests. Compose `kuma()`
labels become inert the moment the docker source is off — they are left in place and die
with each role's retirement (removing 100+ call sites up front is churn without effect;
noted in the role CLAUDE.md so nobody reads an inert label as live).

### KD2 — The docker-type monitors convert to ONE fleet push check; the socket stays private

Branch (b) of the inventory's fork. M1 stands — no LAN exposure of docker-proxy, no third
proxy. The ~25 per-container docker monitors (excluding the kuma/autokuma selves, which the
move replaces) are superseded by **one `docker-fleet` push monitor** fed by a host cron on
daniel-server (`state_push.py` pattern): compare `docker ps` running+health against an
Ansible-rendered expected list (from `containers_list` + the known sidecars), push up/down
with the failing names in the message. Per-container Kuma tiles for the residual set go
away — accepted: their history dies with the docker source anyway, restart/OOM/CPU detail
already lives in monitor-bridge's cAdvisor checks, and per-name detail survives in the push
message and the Discord alert. The cron also closes the `autoheal`-flap blind spot the
docker monitors covered (a container autoheal keeps bouncing shows as repeated down/up).

### KD3 — Convert BEFORE moving (two steps, one moving part each)

Step 1 lands while Kuma is still Docker: remove the docker `kuma()` branches, deploy the
fleet cron + its push monitor, let label-driven AutoKuma delete the old docker monitors
(`on_delete=delete` is current behavior; the in-place type-change wedge — the known
stats-cache quirk — is avoided by delete+create, not conversion). Verify Discord still
fires. Only then does step 2 move a Kuma whose DB no longer holds docker-type monitors —
which also defuses the `kuma_docker_host` FK (the record loses its last referents; it rides
along dead and gets deleted in the UI post-move).

### KD4 — AutoKuma is the first true k8s sidecar (same pod)

Docker runs it with `depends_on: {condition: service_started, restart: true}` because
recreating Kuma invalidates AutoKuma's Socket.IO session. Same-pod two-container reproduces
exactly that: a pod recreate cycles both. `AUTOKUMA__KUMA__URL=http://localhost:3001`. The
password keeps the file-not-env discipline (Secret mounted, entrypoint wrapper unchanged in
spirit — k8s Secret volume + the same `sh -c 'export …=$(cat …)'` wrapper). Image gets
pinned at the move (`:latest` today; the k8s-defaults Renovate manager needs a version to
track).

### KD5 — Notifications: verify static-file support FIRST; two fallbacks

Gate before any cutover work: stand the cluster pair up against a scratch Kuma (or the live
one in read-only intent) and prove a static `.toml` can define the `discord` + `email`
notification entities and that `notification_name_list` resolves. Fallback 1: AutoKuma's
Kubernetes source for the two notification CRs only (docker off, static files for
monitors). Fallback 2: provision both notifications once by hand in the new Kuma UI and
reference by name — acceptable because they change ~never and the secret stays in SOPS
either way. If none resolves names of unmanaged entities, fallback 2 collapses into
"AutoKuma manages nothing but monitors" and the notification link test (KD7) guards it.

### KD6 — Hostname, push URLs, and the one-variable flip

The service keeps `hostname: uptime-kuma` semantics via the established pattern:
daniel-box entry `uptime-kuma{{ k8s_hostname_suffix }}` + `bridge_hostname: uptime-kuma`,
so every routed push URL (`https://uptime-kuma.local.<domain>/api/push/…`) keeps working
unchanged — first through the forward bridge, natively once DNS drains. The k8s Authelia
config already carries the `/api/push/` bypass for the unsuffixed name; the `-k8s` name
needs the same bypass added. Cutover flips:
1. `monitoring_controller_host: daniel-box` — repoints the 8 `--resolve` pinners
   (daniel-box's `server_ip` answers 443 via the traefik Service externalIP since B5).
2. The 6 Docker-DNS sites re-point per role to the `-k8s` name (the N8N lesson: `-k8s`,
   never the bridged name, from containers on daniel-server — hairpin). monitor-bridge and
   autofix-bridge are env flips; cloudflare-ddns (Docker) likewise; the prometheus scrape
   job and homepage widget re-target the `-k8s` name; `check.py`/`autofix.py` defaults move
   too.
Tokens are unchanged everywhere — monitors and their history survive the URL flip.

### KD7 — Test surface moves with the declarations

`test_push_monitor_retries.py` greps compose templates for `kuma(` — after KD1 its corpus
must include the static-monitors template or the max_retries=0 guard silently loses all
its subjects. Same for `validate_compose_templates.py`'s macro render and
`_render_guard.py`'s `kuma_docker_host` stub (which retires with the FK). New guards: every
static monitor carries a notification link (KD5's silent-failure mode), and the fleet
cron's expected list stays derived from inventory (not hand-maintained).

## Execution order (each step verified before the next)

1. **KD5 gate** — prove static-file notifications (or pick the fallback) on a scratch
   deploy. Nothing else starts until this answers.
2. **KD2/KD3 step 1** — fleet cron + `docker-fleet` push monitor on daniel-server; remove
   the docker `kuma()` branches; verify deletion + a forced down alert.
3. **Build the k8s role** — uptime-kuma + autokuma sidecar pod, static-monitors ConfigMap
   (everything moves verbatim except the dead docker types), PVC (1 Gi, `longhorn` —
   backed up, which the Docker copy never was; measure `data/` first), Authelia `-k8s`
   bypass, ingressroute with `bridge_hostname`.
4. **Cutover window** — stop Docker kuma+autokuma; tar-snapshot `data/` (the only rollback
   artifact); force-seed; deploy the cluster pair; verify monitors, notification, a real
   Discord test alert; verify AutoKuma reconciles cleanly against the seeded DB.
5. **Flip the pushes** — `monitoring_controller_host` + the 6 per-role re-points; watch
   every push monitor beat once (their `push_token`s are the proof the flip worked).
6. **Retire** — Docker uptime-kuma role to archive, inventory entry swap (19 → 18),
   `kuma_docker_host` + `_render_guard` stub removed, count lineage, docs.

## KD5 gate — PASSED 2026-08-10 (runtime-proven, then source-verified)

A second AutoKuma v2.0.0 ran against live Kuma with `AUTOKUMA__DOCKER__ENABLED=false` and a
static dir defining a notification + a push monitor referencing it: both created ("Creating
new notification/push" in its logs), no docker socket, no crash. Source-level confirmations
(v2.0.0 tag): `Entity` enum parses `type = "notification"` from files (entity.rs);
`docker.enabled` exists in stable despite being absent from the stable README (config.rs,
default true); `on_delete` defaults to Delete; upstream ships `monitors/example_notification.json`.
Managed = the instance's OWN sqlite DB ∩ live entities (kuma.rs) — which makes additive
testing safe AND means the cutover must seed autokuma's own DB alongside Kuma's, with
static filenames equal to the existing label-derived ids, or reconciliation duplicates the
fleet. The 5s "Updating notification" churn quirk affects file-defined notifications too.
Gate entities removed afterwards via the kuma-cli release binary; DB verified clean.

## Step 2 — EXECUTED 2026-08-10 (KD2/KD3)

`kuma_docker_monitors: false` + macro gate + fleet cron landed (one commit), full deploy
re-rendered the fleet. Verified: `SELECT type, COUNT(*)` shows **zero docker-type monitors**
(85 remain: 56 push / 25 http / 2 port / 1 ping / 1 dns), `Docker Fleet Health` active with
two consecutive 5-min up beats ("30 containers running, none unhealthy"), Discord delivery
check green, all 11 Prometheus targets up. The Kuma DB now carries no docker-type monitor —
the `kuma_docker_host` FK is referent-free ahead of the seed.

## Unverified — resolve during execution
- AutoKuma static-file **field parity** with the label macro (accepted_statuscodes,
  max_redirects, dns fields) — render one of each type on the scratch deploy.
- Whether AutoKuma's `:latest` current version matches the docs' `dev` options
  (`AUTOKUMA__DOCKER__ENABLED` etc.) — pin first, read that tag's docs.
- `data/` actual size before sizing the PVC.
- Whether the seeded Kuma 2.4.0 schema tolerates the container jump identically
  (same image tag pinned in the k8s role — no version skew across the move).
