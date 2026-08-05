# k3s Slice 2 — The Remaining Leaf Apps

Slice 1 proved one stateless service could live in the cluster behind the new Traefik and
Authelia. Slice 2 is the first slice that moves **real user data** and the first that
**turns Docker services off**. Those two facts, not the service count, are what make it
harder than slice 1.

Nine services move. The per-service authoring cost is genuinely low — `ansible/templates/
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

## Scope — nine services

| Service | State | StorageClass | Why |
|---|---|---|---|
| `littlelink` | none | — | Static landing page, public, no Authelia |
| `cloudflare-ddns` | config only | — | Its only mount is the API token → becomes a k8s Secret |
| `speedtest` | 23M SQLite | `longhorn-nobackup` | Real `database.sqlite`, not a cache — but it is observational history, and the Laravel `APP_KEY` lives in SOPS so a rebuilt instance works. **Judgment call:** losing it loses history, not function. One-line change to `longhorn` if that trade is wrong |
| `freshrss` | DB | `longhorn` | Feed subscriptions and read state are not reconstructible |
| `healthchecks` | DB | `longhorn` | Check definitions + ping history |
| `code-server` | workspace | `longhorn` | Working files |
| `n8n` | DB + credentials | `longhorn` | Encrypted credentials; losing them means re-authing every integration |
| `karakeep` | DB + Meilisearch index | `longhorn` | Multi-container; the index is rebuildable but the bookmark DB is not |
| `livesync` | CouchDB | `longhorn` | **Obsidian notes.** Highest-value data in the slice |

Six new backed-up volumes — `freshrss`, `healthchecks`, `code-server`, `n8n`, `karakeep`'s
`./data`, and `livesync`. See *B2 transaction budget* below — that is the constraint most
likely to bite silently.

### Mounts, read from the compose templates

This is what `seed-volume` gets invoked with, so it is recorded rather than inferred. Paths
are relative to `server/containers/<service>/` on daniel-server.

| Service | Carries state | Becomes something else |
|---|---|---|
| `speedtest` | `./config` (23M; `database.sqlite`, `keys`) | — |
| `freshrss` | `./config` | `nginx-feed-cache.conf` → ConfigMap |
| `healthchecks` | `./config` | — |
| `code-server` | `./config` | — |
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
verify the app reads its data → cut over. The Docker service stays stopped-but-defined until
the service is verified, so rollback is `docker compose up`.

### 3. Uptime-Kuma monitors — the same by-hand pattern as slice 1

AutoKuma generates monitors from Docker labels, which do not exist in k8s; the rework belongs
to slice 3. Until then a migrated service loses its monitor at exactly the moment it becomes
least proven. Use the mechanism slice 1 and the telemetry heartbeat already use: a `kuma()`
call on the **uptime-kuma container's own compose file**, which AutoKuma reads normally.
One line per migrated service, deleted when slice 3 reworks the path.

### 4. B2 transaction budget — a checkpoint, not an assumption

Class C transactions were reported at **1,400 by 21.7h into 2026-08-02**, projecting
~1,546/day against a **2,500/day cap** — about 62%. That figure reflects the Longhorn
poll-interval fix (288 → 24/day) having settled.

This slice adds six backed-up volumes. Their recurring-job cost is not known in advance,
which is the whole problem: the cap is enforced by Backblaze, and exceeding it degrades
backups silently.

**Checkpoint:** after Batch C lands (the first two backed-up volumes), re-read Class C from
the B2 console before starting Batch D. If the projection exceeds ~2,000/day, move
`code-server` to `longhorn-nobackup` and revisit the recurring-job schedule rather than
pushing on.

## Batches — thin, each independently exercisable

Ordered so the mechanics are proven on services whose loss would not matter before they are
trusted with data that would.

| Batch | Services | What it proves |
|---|---|---|
| **A** | `littlelink` | The stateless path end-to-end on a public, no-Authelia route whose hostname (`www`) differs from the service name. `ical-proxy` was dropped from this batch on contact with the code. |
| **B** | `cloudflare-ddns`, `speedtest` | Secrets as k8s Secrets, and **`seed-volume` is built and first exercised here** — against a 23M SQLite DB on `longhorn-nobackup`, where a botched copy costs nothing. |
| **C** | `freshrss`, `healthchecks` | `seed-volume` against data that matters, on `longhorn`, with backup verified in B2. **B2 checkpoint here.** |
| **D** | `code-server`, `n8n` | Larger state; n8n's encrypted credentials survive the move. **Blocked — see *Open: image registry*.** |
| **E** | `karakeep`, `livesync` | Multi-container workload, and the highest-value data in the slice, moved last with every mechanic already proven. |

One PR per batch.

## Open: image registry — blocks Batch D

Three services in this slice build their image from a local context rather than pulling a
published one: `code-server`, `n8n`, and `ical-proxy` (already removed for a second reason).
Docker Compose builds them in place; k8s has no build step and must pull from a registry.

Batch D cannot start until this is answered. Three options, none obviously right:

| Option | Cost | Note |
|---|---|---|
| Build and push to GHCR | CI job per image; images become public unless a pull secret is added | Fits the repo's existing GitHub tooling |
| Run an in-cluster registry | One more service to run, back up, and secure | Keeps everything on-LAN |
| Leave those three in Docker | Zero | Concedes that daniel-server keeps a role past slice 7 |

This does not block Batches A, B, C, or E.

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

Per service (all nine):

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

Per stateful service (seven):

- [ ] `seed-volume` reported matching file count, byte count, and checksum
- [ ] The application reads its migrated data — feeds present, checks present, workflows
      present, bookmarks searchable, notes replicating
- [ ] For `longhorn` volumes: `b2-list-longhorn.sh` shows `.blk` blocks for the volume
- [ ] For `longhorn-nobackup` volumes: the Volume CR carries
      `recurring-job-group.longhorn.io/no-backup: enabled` — read from the **Volume CR**, not
      inferred from the StorageClass

Slice-wide:

- [ ] daniel-server container count is **66 − 9 = 57**, and `uptime -s` is still
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
