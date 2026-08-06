# k3s Slice 2 — The Remaining Leaf Apps

Slice 1 proved one stateless service could live in the cluster behind the new Traefik and
Authelia. Slice 2 is the first slice that moves **real user data** and the first that
**turns Docker services off**. Those two facts, not the service count, are what make it
harder than slice 1.

Eight services move. The per-service authoring cost is genuinely low — `ansible/templates/
ingressroute.yml.j2` was written during slice 1 specifically so that "slice 2's ~33
remaining services are a one-line call each" — so batch size here is a scheduling choice,
not a risk decision. The risk lives entirely in data migration and cutover.

## Baseline — captured before the first change

Recorded 2026-08-05 13:01 UTC, from daniel-server:

| Measure | Value |
|---|---|
| Containers running | **66** |
| Containers unhealthy | **0** |
| Boot time (`uptime -s`) | **2026-08-02 07:37:10** |

Slice 1 had to close its "no unexpected change on daniel-server" criterion as *unverifiable*
because no baseline existed. This is that baseline. The count should fall by exactly one per
service retired, and by nothing else.

## What this slice proves, and what it deliberately does not

**Proves:** a stateful service's data survives the move from a Docker bind mount on
daniel-server to a Longhorn PVC on daniel-box, byte-for-byte; a migrated service is
reachable by its *real* hostname from every resolver on the network; and a retired Docker
service leaves nothing behind.

**Does not prove:** anything about monitoring rework (slice 3 owns AutoKuma), the edge path
(slice 6 owns CrowdSec/Pi-hole/router), or resilience — Longhorn still runs at one replica
until daniel-server joins the cluster at slice 7.

## Scope — eight services

| Service | State | StorageClass | Why |
|---|---|---|---|
| `littlelink` | none | — | Static landing page, public, no Authelia |
| `cloudflare-ddns` | config only | — | Its only mount is the API token → becomes a k8s Secret |
| `speedtest` | 23M SQLite | `longhorn-nobackup` | Real `database.sqlite`, not a cache — but it is observational history, and the Laravel `APP_KEY` lives in SOPS so a rebuilt instance works. **Judgment call:** losing it loses history, not function. One-line change to `longhorn` if that trade is wrong |
| `freshrss` | DB | `longhorn` | Feed subscriptions and read state are not reconstructible |
| `healthchecks` | DB | `longhorn` | Check definitions + ping history |
| `n8n` | DB + credentials | `longhorn` | Encrypted credentials; losing them means re-authing every integration |
| `karakeep` | DB + Meilisearch index | `longhorn` | Multi-container; the index is rebuildable but the bookmark DB is not |
| `livesync` | CouchDB | `longhorn` | **Obsidian notes.** Highest-value data in the slice |

Five new backed-up volumes — `freshrss`, `healthchecks`, `n8n`, `karakeep`'s `./data`, and
`livesync`. See *B2 transaction budget* below — that is the constraint most
likely to bite silently.

### Mounts, read from the compose templates

This is what `seed-volume` gets invoked with, so it is recorded rather than inferred. Paths
are relative to `server/containers/<service>/` on daniel-server.

| Service | Carries state | Becomes something else |
|---|---|---|
| `speedtest` | `./config` (23M; `database.sqlite`, `keys`) | — |
| `freshrss` | `./config` | `nginx-feed-cache.conf` → ConfigMap |
| `healthchecks` | `./config` | — |
| `n8n` | `./data` (→ `/home/node/.n8n`), `./local-files` | — |
| `karakeep` | `./data`, and the named volume `karakeep_meili` | `time-tagger/script` → ConfigMap; `time-tagger/uv-cache` → `emptyDir` |
| `livesync` | `./couchdb-data` | `./couchdb-etc` (CouchDB `local.ini`) → ConfigMap/Secret |

`karakeep` is four containers, not one — `karakeep`, `karakeep-chrome` (stateless),
`karakeep-meilisearch`, `karakeep-time-tagger`. The Meilisearch index is rebuildable from the
bookmark DB, so it goes to `longhorn-nobackup` while `./data` goes to `longhorn`.

### Excluded, with the slice that owns each

| Service(s) | Owner |
|---|---|
| `mosquitto`, `zigbee2mqtt`, `home-assistant` | Slice 5 — smart home |
| `prometheus`, `grafana`, `uptime-kuma`, `otel-collector`, `monitor-bridge`, `autofix-bridge` | Slice 3 — monitoring cluster |
| The nine media services | Slice 4 |
| `crowdsec` (Metabase) | Slice 6 — kept with the LAPI rework it reports on |
| `pihole`, `wg-easy`, `homelab-mcp` | Last — they are the instruments used to operate the migration |
| `terraria`, `terraria-stats` | Deferred — `terraria` is a raw-TCP rework, and `terraria-stats` reads its console from Loki, which moves in slice 3 |
| `ical-proxy` | **Removed from this slice 2026-08-05** — moves *with* `homepage`. See below |
| `code-server` | **Removed from this slice 2026-08-05** — it is a rework, not a port. See below |
| `portainer`, `docker-proxy`×3, `autoheal`, `watchtower`, `glances` | Dissolve into platform primitives |
| `peanut`, `scrutiny` | Pinned by hardware |
| `kopia` | Rework |

**`homepage` is blocked, not scheduled.** It queries `portainer:9000` directly, so its widget
set depends on the unresolved Portainer decision. Moving it before that is decided means
authoring a config we then rewrite.

**`ical-proxy` was removed on 2026-08-05, on contact with the code.** Two independent
blockers, neither visible from the design doc:

1. It has no published image. Its compose uses `build:` against a local context, and there is
   no registry in the cluster for k8s to pull from.
2. `homepage` reaches it as `http://ical-proxy:5000/calendar1.ics` over the `homepage_private`
   Docker bridge. No such path exists from a Docker container on daniel-server to a ClusterIP
   on daniel-box, so migrating `ical-proxy` alone breaks the homepage calendar widgets.

Both dissolve if it moves together with `homepage`, so that is where it goes.

**`code-server` was removed on 2026-08-05 for a reason the registry does not fix.** It sets
`DOCKER_HOST: tcp://docker-proxy-codeserver:2375` — a dedicated read-only Docker socket proxy
on a private network — and `docker-proxy` is on the *dissolve* list, replaced by RBAC and
ServiceAccounts. So porting code-server means answering a design question first: what does a
code-server pod talk to instead, and with what permissions? That is a rework, and reworks are
not what this slice is for. Its heavy image (LaTeX, Node 22, a Python venv, Claude Code CLI,
VSIX extensions baked at build time) will still need the registry when it does move.

## Decisions settled here

### 1. Cutover changes no DNS — a strangler bridge on daniel-server

This is the tightest constraint in the slice and it gates every cutover. The first framing
below turned out to be incomplete; both it and the correction are kept, because the reasoning
that was wrong is the reasoning someone will otherwise repeat.

Verified 2026-08-05:

- `dig +short <anything>.local.daniel-hunter.com @1.1.1.1` → **`10.0.0.161`** (daniel-server).
  A public wildcard serving an RFC1918 address — the existing split-horizon trick.
- `dig +short bento-pdf-k8s.local.daniel-hunter.com @10.0.0.161` → **`10.0.0.240`**.
  Pi-hole carries correct per-host overrides.

Dual-run works today only because the k8s twin answers on a *different* hostname
(`k8s_hostname_suffix: "-k8s"`). The moment a Docker copy stops and its real hostname is
repointed, any client not resolving through Pi-hole lands on daniel-server with nothing
listening.

Who that is:
- **Fine:** LAN clients on Pi-hole via DHCP, and mobile full-tunnel WireGuard clients —
  `wg-easy` pushes `default_dns: {{ server_ip }}`.
- **Breaks:** desktop split-tunnel WireGuard clients, which the wg-easy host_vars comment
  states "override it (hosts file) to keep general DNS on Mullvad", plus any LAN client with
  a hardcoded resolver.

**Revised 2026-08-05, before any cutover.** The paragraph above framed this as a LAN problem.
It is not — every service also has a *public* route, and that changes the answer.

`ansible/templates/traefik.yml.j2` gives every Docker service **two** host rules:
``Host(`<name>.<domain>`) || Host(`<name>.local.<domain>`)``. Measured the same day, all eight
slice-2 services that carry a web route — plus the already-migrated `bento-pdf` — resolve
publicly to `104.21.29.113` / `172.67.148.199`, Cloudflare proxy IPs. (`cloudflare-ddns` has
no route, so it is the one service unaffected.) So a proxied `*.<domain>` wildcard
sends public traffic to the home IP, the router forwards `:443` to daniel-server, and Docker
Traefik terminates it.

Stopping a Docker copy therefore breaks **public** access, not just LAN — and the router
forward belongs to slice 6. A per-host A record to `10.0.0.240` cannot fix that: the public
names are Cloudflare-*proxied*, and Cloudflare cannot reach an RFC1918 origin.

**Decision — strangler bridge.** Keep Docker Traefik as the edge for the whole of slice 2.
When a service's workload moves, replace its container with a **file-provider router on
daniel-server** carrying the same two host rules and forwarding to the k8s VIP. The mechanism
already exists: the Docker side runs hand-rolled file-provider routers today, which is why
`ingressroute.yml.j2` documents matching their TLS block.

This is strictly better than the DNS plan it replaces:

- **No DNS change at cutover, LAN or public.** Both wildcards keep pointing where they do now,
  so the hosts-file question and the split-tunnel WireGuard breakage both disappear.
- **Rollback is one router flip**, not a DNS propagation wait.
- **Slice 6 removes every bridge at once** when the router forward moves to the VIP.

Cost: one extra hop per request, and Docker Traefik stays load-bearing — which it already is
until slice 6 regardless. Authelia stays gated at the **k8s** layer only; the bridge router
must not also attach the Docker forward-auth, or a user hits two portals with two cookies.

### 2. Data migration — one reusable seeding role, not ten bespoke copies

Same reasoning as the ingressroute macro: write it once. Add `ansible/roles/k8s/seed-volume/`,
which takes a service name, a source path on daniel-server, and a PVC, then:

1. Creates the PVC on the declared StorageClass.
2. Starts a short-lived seed pod mounting it.
3. Streams the source directory in as a tar over the existing SSH trust — no intermediate
   copy on disk.
4. **Verifies before declaring success:** file count and total bytes match the source, and
   a recursive checksum agrees.
5. Deletes the seed pod.

Step 4 is the point of the role. A silent partial copy is the failure mode that matters —
`livesync` losing notes, `n8n` losing credentials — and it is invisible without an explicit
comparison.

Every stateful migration is: stop the Docker service → seed → start the k8s workload →
verify the app reads its data → cut over.

**Then `docker rm` the containers — stopping is not enough.** This paragraph used to say the
Docker service stays stopped-but-defined for rollback, and that is wrong in a way nothing caught
for five cutovers: AutoKuma generates a monitor from a **stopped** container just as readily as a
running one, and it then fails every beat with `Container State is exited`. The earlier cutovers
never showed it because those containers happened to be removed, at which point AutoKuma deletes
the monitor within a couple of minutes. karakeep stopped two and immediately had two monitors
paging. Rollback is unaffected by the removal: the compose file and the data directory both stay
on disk, so `docker compose up -d` under `containers/<service>/` still rebuilds it.

### 3. Uptime-Kuma monitors — the same by-hand pattern as slice 1

AutoKuma generates monitors from Docker labels, which do not exist in k8s; the rework belongs
to slice 3. Until then a migrated service loses its monitor at exactly the moment it becomes
least proven. Use the mechanism slice 1 and the telemetry heartbeat already use: a `kuma()`
call on the **uptime-kuma container's own compose file**, which AutoKuma reads normally.
One line per migrated service, deleted when slice 3 reworks the path.

**A push monitor cannot referee dual-run.** Where a service pushes its own heartbeat —
`cloudflare-ddns` is the only one in this slice — both copies push the same token to the same
monitor, so either one alone keeps it green. During coexistence that monitor proves "at least
one copy is alive" and nothing more. Verify the k8s copy from its own pod logs instead, and
treat the monitor as meaningful again only once the Docker copy is gone.

**Unrouted workloads lose their alert entirely, and karakeep made that a fleet.** The by-hand
pattern above only works for something an HTTP monitor can reach. `registry` and
`cloudflare-ddns` were the first cases; `karakeep` retired three at once — `karakeep-chrome`,
`karakeep-meilisearch` and `karakeep-time-tagger` all had `docker`-type AutoKuma monitors that
died with their containers. In-cluster their liveness probes restart them, which is strictly
better than autoheal was, but *nothing pages*: a crash-looping Meilisearch degrades search while
`karakeep (via bridge)` stays green.

Ruled out explicitly, so nobody reaches for it later: giving `karakeep-chrome` an ingress to
make it probeable would put Chrome DevTools Protocol on port 9222 on the LAN, which is arbitrary
code execution and local file read for anyone who can reach it. The helpers are unrouted on
purpose.

The general answer is a cluster-side pod-health push — one cron in the `k3s` role reading pod
readiness and pushing a single Kuma monitor, the same state-file-to-monitor idiom
`longhorn-backup-health.sh` already uses. Deliberately **not** built under karakeep's cutover:
built for one service it would take that service's shape and need redoing for the other five.
Pending, and now covering six workloads rather than two.

### 4. B2 transaction budget — a checkpoint, not an assumption

Class C transactions were reported at **1,400 by 21.7h into 2026-08-02**, projecting
~1,546/day against a **2,500/day cap** — about 62%. That figure reflects the Longhorn
poll-interval fix (288 → 24/day) having settled.

This slice adds six backed-up volumes. Their recurring-job cost is not known in advance,
which is the whole problem: the cap is enforced by Backblaze, and exceeding it degrades
backups silently.

**Checkpoint:** after Batch C lands (the first two backed-up volumes), re-read Class C from
the B2 console before starting Batch D. If the projection exceeds ~2,000/day, revisit the
recurring-job schedule before adding more backed-up volumes rather than pushing on — of what
remains, only `karakeep`'s Meilisearch index is genuinely droppable, and it is already on
`longhorn-nobackup`.

## Batches — thin, each independently exercisable

Ordered so the mechanics are proven on services whose loss would not matter before they are
trusted with data that would.

| Batch | Status | Services | What it proves |
|---|---|---|---|
| **A** | **Done 2026-08-05** (#81) | `littlelink` | The stateless path end-to-end on a public, no-Authelia route whose hostname (`www`) differs from the service name. `ical-proxy` was dropped from this batch on contact with the code. |
| **B** | **Done 2026-08-05** (#83) | `cloudflare-ddns`, `speedtest` | Secrets as k8s Secrets, and **`seed-volume` is built and first exercised here** — against a 23M SQLite DB on `longhorn-nobackup`, where a botched copy costs nothing. |
| **C** | **Done 2026-08-05** (#84) | `freshrss`, `healthchecks` | `seed-volume` against data that matters, on `longhorn`, with backup verified in B2. **B2 checkpoint here.** |
| **R** | **Registry done 2026-08-05** (#86); builder deferred to D | `registry` + builder | Infrastructure, not a migration: `registry:2` plus an in-cluster build Job. Must land before D, which is the first batch needing a built image. |
| **D** | Blocked — the builder | `n8n` | Larger state, and the first built image — two of them, main and task runners. n8n's encrypted credentials survive the move. |
| **E** | `livesync` **done 2026-08-06** (#100, #101); `karakeep` remaining | `karakeep`, `livesync` | Multi-container workload, and the highest-value data in the slice, moved last with every mechanic already proven. `livesync` proved the bridge can carry a service whose edge routing is not generatable. |

One PR per batch.

### The bridge, as built

Decision 1 above is implemented. Setting `bridge_hostname` on a service's daniel-box inventory
entry does two things, and the pair is what a cutover *is*:

- the `ingressroute()` macro emits a **second route** on that service's IngressRoute, matching
  the unsuffixed hostnames — the ones the Docker copy used to answer;
- the traefik role's `config.yml.j2` reads that same inventory and emits a **file-provider
  router per hostname** on daniel-server, forwarding to the VIP.

Two things were settled by measurement rather than design, and both changed the shape:

- **The bridged k8s route carries no forward-auth, and is guarded by `ClientIP` instead.**
  Gating both ends means two portals and two cookies; worse, the k8s portal is
  `auth-k8s.<domain>`, which has no public DNS record while `k8s_public_route` is false, so a
  public visitor would be redirected somewhere that does not resolve. Authelia therefore stays
  at the Docker edge. That leaves an un-gated route on the cluster, which `ClientIP` closes:
  only daniel-server can reach it. The matcher is sound because Traefik implements it with
  `ip.RemoteAddrStrategy` — the TCP peer, not `X-Forwarded-For`, which matters because
  `lan_subnet` *is* a trusted forwarded-header source and an XFF-based matcher would have been
  forgeable by exactly the hosts it keeps out.
- **One router per hostname, not one router with an `||` rule.** The backend is HTTPS, and
  Traefik answers 421 when the Host header disagrees with the SNI. Measured against the live
  cluster: an apex `serverName` with a per-service Host 421s on both HTTP/2 *and* HTTP/1.1,
  and so does an IP URL with no SNI at all. `serversTransport.serverName` holds one value, so
  two hostnames need two routers.

Bridged routes also use `rate-limit-bridge` rather than `rate-limit`: every request arrives
from one peer by construction, so the default source criterion would meter all users of a
bridged service into a single bucket.

A bridged service that sits behind Authelia has a monitoring problem the bridge creates: the
edge answers 302 *before* consulting the bridge, so a monitor on `/` stays green whether or not
daniel-server can still reach the cluster. `bridge_probe_path` closes it — a LAN-only,
auth-skipping router on one exact path, so a 200 can only have come from the app. It is
LAN-only by enforcement rather than by comment: there is no public sibling, and the
forward-auth test's exemption for probe routers is conditional on the test that asserts it.

A service can also arrive with an unauthenticated path it needs to *keep*.
`bridge_bypass_prefixes` reproduces one on the bridge: public, `PathPrefix`, no forward-auth,
one router per hostname. healthchecks' `/ping/` is the case — every monitored cron posts to it
without credentials, and two crons in `initial_setup` hardcode the public URL — and it was a
Docker label on the container, so it died with the container rather than degrading. Unlike a
probe path this is a deliberate hole in the edge, so each prefix must also be declared in
`BRIDGE_BYPASS_PREFIXES` in the test with a written reason; inventory alone cannot open one.
The trailing slash is part of the contract, since `PathPrefix('/ping')` would also match
`/pingXYZ`.

`littlelink` needs none of that, and is worth keeping in mind for it. Being public and
un-gated, its ordinary bridge monitor already reaches the app, so it is the canary for the
bridge mechanism itself — it goes red if the mechanism breaks, independently of whether the
gated services' probe paths are still right.

`ansible/tests/test_strangler_bridge.py` holds the parts that span both inventories — a
bridged service must have no Docker container left, must have a k8s route behind it, and must
send an SNI matching the Host it serves.

### Where it stands, 2026-08-06

**Every routed service in the slice has cut over** — `cloudflare-ddns`, `speedtest`,
`littlelink`, `freshrss`, `healthchecks` and now `livesync`. daniel-server is at 41 managed
containers (46 at the start, less six cut over, plus `tempo` added in parallel), and none of the
seven workloads in the cluster has a Docker twin serving traffic. What remains of slice 2 is
`karakeep` and batch D.

`freshrss` needed no config change at all, which is worth recording because it was the one
expected to: FreshRSS keeps its `base_url` in the seeded `config.php`, and that file was
written by the Docker copy, so it already read `https://freshrss.<domain>`. The cutover made
it correct rather than requiring an edit.

| Service | Seeded | Route (at the VIP) | Monitor |
|---|---|---|---|
| `littlelink` | — | **cut over** — bridged, 200 (public, no Authelia) | `littlelink-k8s`, `littlelink-bridge` |
| `speedtest` | 43 files, identical digest | **cut over** — bridged | `speedtest-k8s`, `speedtest-bridge` |
| `cloudflare-ddns` | — | no route (headless) | shares the Docker twin's push token — see below |
| `freshrss` | **730 files, identical digest** | **cut over** — bridged | `freshrss-k8s`, `freshrss-bridge` |
| `healthchecks` | 2 files, identical digest | **cut over** — bridged, `/ping/` bypassed | `healthchecks-k8s`, `healthchecks-bridge` |
| `livesync` | source held still, digests matched | **cut over** — bridged, four hand-written routers | `livesync-k8s`, `livesync-bridge` |
| `registry` | — | loopback only, refused on LAN | none — see below |

`livesync` is the one bridge whose edge routing could not be generated, and the shape is worth
recording because `karakeep` has a milder version of the same problem. Its sync API is gated on a
secret `X-Sync-Token` header and `/_utils` is carved out behind Authelia at priority 200, so the
generic `Host(...)` router would have forwarded traffic straight past the token check.
`bridge_custom_routers` suppresses that router *and only that router* — the service and
`serversTransport` are still generated, and a guard test requires something to reference them for
both hostnames, because a suppressed router with no replacement is unreachable in a way every
other guard reads as green.

The token deliberately did **not** follow the service into the cluster. `config.yml.j2` is mode
0600, and the reason that router lives there rather than on a container label is that any
container on `apps` can read labels through the docker-proxy; a k8s Secret is base64 and readable
by anything with namespace API access. The gate stays at the Docker edge and the cluster-side
route is guarded by `ClientIP`.

Measured after cutover, on both hostnames:

| request | answer | what it proves |
|---|---|---|
| no token | 404 | no router matches — the gate holds |
| with token | 401 | matched, crossed the bridge, and CouchDB demanded its own auth |
| `/_utils` | 302 | Authelia still wins at priority 200 |
| `/` on `.local` | 401 | the probe route, end to end |
| `/` public | 404 | the probe route is LAN-only, enforced rather than asserted |

Its `bridge_probe_path` is `/`, and it is the only gated service whose probe is end-to-end
without being engineered to be. `require_valid_user` makes CouchDB answer 401 rather than
redirect, so a 401 on that route was produced by the app, in the cluster, across the bridge —
the quality of signal only `littlelink` otherwise gives.

Two services have no monitor of their own, both for the same structural reason: nothing on
daniel-server can see inside the cluster.

- **`cloudflare-ddns`** pushes its own heartbeat, and both copies push the *same* token to the
  same monitor, so either one alone keeps it green. During coexistence that monitor means "at
  least one copy is alive" and nothing more.
- **`registry`** is bound to daniel-box's loopback and is unreachable from the monitoring host
  by construction. Watching it needs a push cron on daniel-box, the shape
  `longhorn-backup-health.sh` and `telemetry-health.sh` already use. Deferred deliberately: a
  dead registry fails a deploy loudly, at the moment of the deploy, so it is not the silent
  class of failure those crons exist to catch.

### Still missing

- **The builder.** Batch R shipped the registry alone. Nothing has been built or pushed, so
  containerd's pull path through `registries.yaml` is configured but unproven. This is now the
  only thing blocking D. Two design questions are still open: the push has to use the ClusterIP
  Service name while `image:` stays `localhost:5000/...` to match `registries.yaml`, which one
  variable cannot serve; and how the build context reaches daniel-box (ConfigMap, hostPath, or a
  git-clone initContainer) is unwritten.
- **`karakeep`**, the remainder of batch E. It needs no built image — every one of its four
  images is upstream — so it is not behind the builder.

The strangler bridge was the other entry here and is built; see *The bridge, as built* above.

### The B2 checkpoint — resolved 2026-08-06, passed

Read twice on the day, against the ~2,000/day threshold and the 2,500 cap:

| | Class C | Notes |
|---|---|---|
| 02:47 UTC | 155 | before the 03:30 UTC Longhorn run |
| 12:06 UTC | 1,000 | after it, and after the day's Kopia jobs |

Two ways of projecting, both under: scaling 1,000 uniformly over 12.1h gives ~1,983/day, and
adding the remaining 11.9h at the pre-backup drip rate to a day whose scheduled work is already
spent gives ~1,660. The verdict does not depend on which is right.

That reading is also the **worst case for the batch-C volumes**: the 03:30 run backed up four
volumes where every prior day backed up two, and both additions were first-fulls
(`freshrss-config` 324 MiB, `healthchecks-config` 74 MiB, both `Completed`). Every subsequent
run is a block-level incremental.

Two caveats worth carrying:

- It was a **light Kopia day** — no weekly verify (Wednesday), no monthly restore drill (1st), no
  quarterly deep verify. The real stress test is a Wednesday, where the weekly verify overlaps a
  steady-state four-volume Longhorn run.
- **`b2-usage.sh` exports bytes, not transactions** (`kopia_b2_billable_bytes` is the only gauge),
  so this checkpoint needs a human reading the B2 console. A Class C gauge would make it a
  Prometheus query with an alert threshold.

**Batch E does not grow the backed-up set**, which contradicts what the entry here used to say.
`livesync`'s data is on `longhorn-nobackup` — `kopiaignore.j2` already excludes
`livesync/couchdb-data/`, and every reason carries over to Longhorn: a CouchDB B-tree rewritten
daily dedupes poorly, and it is named there as the engine behind the B2 hidden-version churn that
tripped the 85% alert (66M → 1.1G after the 2026-06-23 rebuild). The vault's source of truth is
the markdown on each Obsidian client. `karakeep` still adds one backed-up volume (`./data`); its
Meilisearch index is already destined for `longhorn-nobackup`.

### A bridged request is metered twice

The one that actually reached a user. A bridged request passes the Docker edge router's
middlewares *and* the cluster route's, and where both impose a rate limit the tighter ceiling
wins. `livesync` carries `rate-limit-livesync` at 6000/min at the edge — a deliberate 20x, because
CouchDB replication is extremely chatty — and then met `rate-limit-bridge`'s 300/min inside the
cluster. A phone doing a full pull got 429s within the hour of cutover.

So a service the Docker side exempted from the default ceiling needs the same exemption on its
bridge route, or the migration silently reinstates exactly what the exemption existed to remove.
`bridge_rate_limit` names the cluster-side middleware; a guard derives the requirement from the
rendered edge config, so any bridged service with a `rate-limit-<name>` of its own must set it.

The verification lesson is the sharper half. Every *routing* property was tested before this was
called done — token gate, Authelia carve-out, probe path, SNI, public-vs-LAN — and all of it was
correct. A rate limiter is invisible to single-request checks. **A cutover is not verified until
something has been sent through it at volume**; for anything replication-shaped, a burst of a few
hundred requests is the check that would have caught this.

### Three things a compose file implies that a manifest does not

The first two cost time on `livesync`, the third on `karakeep`, and all three will recur —
`n8n` carries the same assumptions.

**A capability set copied from `cap_add` can still be short, because the volume is different.**
livesync crash-looped with exit 1 and *no log output at all*, which reads like a broken image.
`bash -x` on the entrypoint put the last line at
`find /opt/couchdb/data -type d ! -perm 0755 -exec chmod -f 0755 {} +`. An ext4 Longhorn PVC
carries a `lost+found` at mode 0700; the entrypoint's preceding pass chowns it to the app user,
so root stops owning it, and chmod on a file you do not own needs `FOWNER`. chmod fails EPERM,
`-f` suppresses the message but *not* the exit status, find propagates it, and `set -e` ends the
script before the app writes a line. daniel-server never hits this because a bind-mounted tree is
already all-0755, so the predicate matches nothing. Any image whose entrypoint normalises
ownership or permissions on its data directory is exposed to this the moment the directory
becomes a PVC.

**A compose `healthcheck` is not a k8s probe, and the gap is silent.** livesync's healthcheck is
`curl -s .../` with no `-f`, so it passes on ANY response — including the 401 that
`require_valid_user` returns for every endpoint. Translating it to an `httpGet` probe would have
left the pod permanently not-ready, because k8s treats 401 as failure. Check what the endpoint
actually returns rather than what the healthcheck tolerates; where credentials are needed, an
`exec` probe can read them from the container's own env and keep them out of the manifest.

**`depends_on: condition: service_healthy` has no k8s equivalent, and its absence is not a
one-off error.** karakeep's time-tagger is gated on the app being healthy; k8s starts pods in
parallel, so the tagger raced it and died with `[Errno 111] Connection refused` against
`http://karakeep:3000/api/v1/users/me`. It would not have stayed one error either: the script
touches `/tmp/healthy` only after a *successful* run, so the liveness probe finds no file,
restarts the pod, and re-runs its `uv pip install` — a restart loop that only resolves if a run
happens to land after the app comes up.

The faithful translation is an initContainer that TCP-connects to the dependency's **Service**,
not to a pod. A Service has no endpoints until readiness passes, so the connect succeeds exactly
when `service_healthy` would have — it is the same gate, not an approximation of it. Both
karakeep deployments that had a `depends_on` health gate now carry one. `n8n` has task runners
with the same shape.

### A re-seed has to quiesce the destination, not just the source

`seed-volume`'s header has always said the source must be stopped. It said nothing about the
destination, which is fine for a first seed — the workload does not exist yet — and wrong for
the cutover seed, by which point the k8s copy has been serving and writing for hours. karakeep
made this concrete: the app writes `db.db` continuously and the tagger wakes every 15 minutes.

The failure would not have been corruption. The role checksums the volume after copying, so a
destination that kept writing fails the comparison and the marker is never written — it just
fails every time, and the message reads as a copy bug rather than a running pod. `seed_volume_quiesce`
takes the Deployments that write to the claim and scales them to zero across the copy. The
restore is in an `always`, because a copy that fails with the app scaled to zero is a silent
outage: the play aborts before `k8s/manifests` would re-apply the Deployment.

Also worth stating plainly, since both taggers ran against the same Gemini key during
coexistence: the cutover re-seed **discards** the k8s tagger's work. Docker stayed the source of
truth until the moment it stopped, so that is correct rather than data loss — but it is a reason
not to linger in coexistence longer than the verification needs.

### A `-k8s` hostname is not created by adding it to inventory

Pi-hole's dnsmasq records for `*-k8s.local.<domain>` are generated from **daniel-box's**
`containers_list`, but the template lives in the **pihole role on daniel-server**. Adding the
inventory entry does not create the record; deploying pihole does. Until that ran, both
`livesync-k8s` and `karakeep-k8s` resolved to 10.0.0.161 through the `local.<domain>` wildcard
instead of the ingress VIP, and their Kuma monitors 404'd against daniel-server.

**`curl --resolve` cannot catch this, and is how it survived verification.** `--resolve` bypasses
DNS, so it proves the route works and never that the name points at it. Check the name and the
route separately: `dig +short <name> A @10.0.0.161` should return the VIP, and the request should
be made from a host that resolves through Pi-hole.

### A property of the cluster node worth remembering

daniel-box does not resolve through Pi-hole — its resolver is the ISP's, so it gets the public
wildcard answer for any `*.local.<domain>` name and lands on daniel-server. This has now caused
three separate confusions: services appearing to 404 when probed from the node, `cloudflare-ddns`
being unable to reach `uptime-kuma.local.<domain>` from a pod (fixed with `hostAliases`), and the
original wildcard-DNS finding behind decision 1. Anything on daniel-box or inside a pod that
needs a homelab hostname must pin it explicitly. Slice 3 moves the monitoring stack, which is
full of cross-service hostnames — expect this again there.

### Check what depends on a service before retiring it, not just what it depends on

Retiring `speedtest` and `freshrss` broke Homepage's widgets for both, and it took an hour to
notice. Homepage called them container-to-container — `http://freshrss:80`, `http://speedtest:80`
— over a Docker network, and that name stopped resolving the moment the container went:

```
<freshrssProxyHandler> Error: queryAaaa ENOTFOUND freshrss
<credentialedProxyHandler> HTTP Error 500 calling http://speedtest/api/speedtest/latest
```

Nothing else showed it. Both services were healthy, every bridge check was green, and the
`href` links worked — the only evidence was in the *caller's* logs, which nothing was watching.
That is the shape of the failure: it is silent at the thing being migrated and visible only at
something else entirely.

The pre-cutover checklist was asking what a service depends on. It also has to ask what depends
on it:

```bash
grep -rnE "https?://<service>[:/]" ansible/roles/containers/
```

Run against the *other* roles, not the service's own. Doing this for all five migrated services
found Homepage and nothing else, so the fix was contained — but it was found after the fact
rather than before.

Two things follow. A caller inside the homelab cannot pass Authelia, which is why
`bridge_lan_prefixes` exists; and it will not usually be able to, so expect to add one per
in-house consumer rather than treating it as an exception. **Slice 3 is where this gets
expensive** — the monitoring stack is mostly cross-service calls, and Prometheus scraping a
migrated target by container name fails exactly this quietly.

### 5. Image registry — in-cluster, decided 2026-08-05

Some services build their image from a local context rather than pulling a published one.
Docker Compose builds them in place; k8s has no build step and must pull from somewhere.
Checked rather than assumed, that is **four images**, not three:

| Image | What the build does |
|---|---|
| `n8n` | `npm install -g fuzzball` on `n8nio/n8n:latest` |
| `n8n` task runners | `pnpm add fuzzball` into `/opt/runners/task-runner-javascript`, plus a config file |
| `code-server` | LaTeX, Node 22, a Python venv, Claude Code CLI, VSIX extensions fetched at build time |
| `ical-proxy` | A small Python app from `files/app.py` |

Only n8n's *main* image is plausibly replaceable by an initContainer. The runners image
installs into a path **inside** the image, which an overlay mount would shadow — faking that
is fragile. Building is the honest answer.

**Decision: run a registry in the cluster.** GHCR was the alternative and was rejected to keep
build artefacts on-LAN. Three things this settles:

- **`registry:2` on a Longhorn PVC, `longhorn-nobackup`.** Contents are reproducible from the
  Dockerfiles in this repo, so backing them up would spend B2 transactions — already at ~62%
  of the cap — on regenerable data.
- **k3s must be told to trust it**, via `/etc/rancher/k3s/registries.yaml`. Plain HTTP is
  acceptable while the cluster is single-node, because the traffic never leaves the box. That
  stops being true when daniel-server joins at slice 7, so write the role with TLS in mind
  rather than retrofitting it under time pressure.
- **A registry does not build.** The builder is the other half: an in-cluster Kaniko or
  BuildKit Job, driven by the same Ansible role that renders the Dockerfile today. Building on
  daniel-server with Docker and pushing would work now and die at slice 7 — so build in-cluster
  from the start.

Cold-boot behaviour, so it is not a surprise: pods referencing the local registry
`ImagePullBackOff` until the registry pod is up, then retry and recover unattended.

This lands as **Batch R**, before Batch D. It is infrastructure, not a service migration, and
it blocks nothing else.

## Per-service work

For each service: `ansible/roles/k8s/<name>/` with `tasks/main.yml` (a `k8s/manifests`
include), plus `deployment.yaml.j2`, `service.yaml.j2`, `ingressroute.yaml.j2` — the
bento-pdf role is the template — and a `defaults/main.yml` carrying image, UID, and the
resource caps lifted from the compose `deploy.resources`. Stateful services add a PVC and a
`seed-volume` invocation.

Carry across from the compose definition, deliberately: the resource caps, the healthcheck
(as readiness/liveness probes), and the `use_authelia` decision with its existing comment —
several of those comments record *why* a service is deliberately unauthenticated
(`littlelink` is public by design; `livesync` because CouchDB's own basic auth cannot pass
Authelia 2FA), and that reasoning must not be lost in translation.

## Exit criteria

Per service (all eight):

- [ ] `kubectl -n homelab get deploy <name>` — desired replicas ready
- [ ] `curl -H 'Host: <name>-k8s.local.<domain>' http://10.0.0.240/` → expected status
- [ ] TLS certificate issued by Let's Encrypt **production**
- [ ] Authelia gate matches the `use_authelia` value in `containers_list` — a `302` to the
      k8s portal where true, a direct `200` where false
- [ ] Uptime-Kuma monitor green
- [ ] After cutover, **both** `https://<name>.<domain>/` (public path, through Cloudflare) and
      `https://<name>.local.<domain>/` return the k8s copy — no DNS record changed
- [ ] The bridge router on daniel-server does **not** attach Docker's Authelia forward-auth
      (one portal, one cookie)
- [ ] Docker copy removed from `containers_list` and the container gone from `docker ps`

Per stateful service (six):

- [ ] `seed-volume` reported matching file count, byte count, and checksum
- [ ] The application reads its migrated data — feeds present, checks present, workflows
      present, bookmarks searchable, notes replicating
- [ ] For `longhorn` volumes: `b2-list-longhorn.sh` shows `.blk` blocks for the volume
- [ ] For `longhorn-nobackup` volumes: the Volume CR carries
      `recurring-job-group.longhorn.io/no-backup: enabled` — read from the **Volume CR**, not
      inferred from the StorageClass

Slice-wide:

- [ ] daniel-server container count is **66 − 8 = 58**, and `uptime -s` is still
      `2026-08-02 07:37:10` (no reboot)
- [ ] `docker ps --filter health=unhealthy -q | wc -l` → `0`
- [ ] B2 Class C daily projection re-read after Batch C and again at slice end, both under
      the 2,500 cap
- [ ] `uv run pytest` and `prek run --all-files` clean
- [ ] **(Owner: Daniel — not verifiable from an agent session.)** A desktop split-tunnel
      WireGuard client and a phone off-LAN both reach a migrated service normally. The
      strangler bridge means neither should need any change; this criterion exists to prove
      that, not to fix it

## Explicitly out of scope

- AutoKuma rework — slice 3
- CrowdSec, Pi-hole, and the router forward — slice 6
- Longhorn replica count — stays at 1 until daniel-server joins at slice 7
- `homepage` — blocked on the Portainer decision
- Retiring the `*.local.<domain>` wildcard — it must keep pointing at daniel-server while
  any Docker service remains
