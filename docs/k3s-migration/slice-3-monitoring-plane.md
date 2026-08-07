# k3s Slice 3 — The Monitoring Plane

**Status:** plan, not yet executed. Written 2026-08-07, **revised the same day before execution.**

**Revision — D3 inverted, and the slice got smaller.** The first draft assumed the cluster Kuma
could reach daniel-server's docker-proxy over TCP. It cannot, and must not. No docker-proxy
publishes a host port (`docker ps` shows an empty `PORTS` column for all four), and
`host_vars/daniel-server.yml:71-75` records why, verbatim:

> NOT on `apps` (Security M1, 2026-07-01): this read-only proxy runs CONTAINERS=1, and
> `GET /containers/{id}/json` returns EVERY container's Env (secrets). haproxy can't body-filter,
> so the only lever is who can reach it.

Reachability *is* the access control. Publishing it to serve a cluster Kuma would expose every
container's environment — including secrets — to the LAN. That is not a trade-off to weigh.

The consequence is that **Kuma's Docker-daemon coupling is collector-side**, the same class as
node-exporter, cadvisor and promtail — so this slice's own seam puts Kuma on the
stays-until-slice-7 side. Kuma and AutoKuma no longer move in slice 3. D4 (recreate Kuma state) and
the parallel-Kuma machinery in D7 drop out with it; both are recorded below rather than deleted,
because a future reader needs to know they were considered and why they became slice-7 concerns.

**Second revision — the gap-closure reorder was wrong, reverted.** Between the two revisions this
doc briefly moved the seven-workload gap to the front, on the theory that `http`/`push` monitors
against cluster services need neither a Docker daemon nor a metrics pipeline. Checked against the
live cluster, that is false:

| Workload | Exposes | Reachable from daniel-server? |
|---|---|---|
| `registry` | ClusterIP `10.43.51.14:5000` | No — cluster-internal |
| `karakeep-chrome` | ClusterIP `10.43.225.164:9222` | No — cluster-internal |
| `karakeep-meilisearch` | ClusterIP `10.43.192.233:7700` | No — cluster-internal |
| `cloudflare-ddns-direct` | **no Service** | Nothing to probe |
| `cloudflare-ddns-proxied` | **no Service** | Nothing to probe |
| `karakeep-time-tagger` | **no Service** | Nothing to probe |
| `n8n-runners` | **no Service** | Nothing to probe |

None of the seven has an IngressRoute (only 11 other workloads do). So there is no endpoint to
`http`, and nothing in-cluster pushing. **Pod readiness for these seven is only available from the
Kubernetes API**, which means kube-state-metrics → Prometheus → a `monitor-bridge` check — and that
is downstream of cluster scraping, exactly where the first draft put it. The original ordering
stands; what was missing was the *reason*, now recorded as D8.

The AutoKuma Files source (D2) is still correct and still verified, but it is no longer what closes
the gap, and it is no longer the front of the slice.

Design doc §8 gives this slice one line: *"Monitoring cluster + bridges (incl. the AutoKuma
rework) — dashboards and alerts equivalent to today's."* That understates it in one direction and
overstates it in another, and both corrections come from the same fact: **the monitoring plane is
the only plane that is coupled to the host it runs on.**

---

## Baseline — captured 2026-08-07, before any change

Read from the live config, not from memory.

**Docker, on daniel-server** (`containers_list` in `host_vars/daniel-server.yml`):

| Service | Role | Note |
|---|---|---|
| `prometheus` | TSDB + scrape | 11 scrape jobs, **no `rule_files`** |
| `grafana` | dashboards | **Loki and promtail live inside this role**, not their own entries |
| `uptime-kuma` | alert brain | 2.4.0; `./data:/app/data` is the entire state |
| `autokuma` | monitor generation | reads `tcp://docker-proxy:2375` |
| `otel-collector` | OTLP receiver | host-loopback for Claude Code |
| `monitor-bridge` | metric → Kuma push | 42 checks, incl. the B2 gate added today |
| `autofix-bridge` | remediation crons | disk prune, fake-remux |

Plus `node-exporter` and `cadvisor`, which are scraped but are not `containers_list` entries of
their own.

**k3s, on daniel-box** — `claude-otel` is already a complete, persistent observability stack in
its own namespace: Prometheus, Grafana, Loki and Tempo, each with a Longhorn PVC, explicit
retention, and an ingress. Its Prometheus scrapes **two jobs**, both the collector itself. It
monitors Claude Code telemetry and nothing else.

**The gap this slice closes:** Docker Prometheus scrapes `prometheus`, `traefik`, `node`,
`cadvisor`, `loki`, `promtail`, `otel-collector`, `uptime-kuma`, `crowdsec`, `terraria-stats`,
`home-assistant` — **nothing in the cluster**. Fourteen k8s workloads have run since slice 2 with
no metrics and, for the seven that have no ingress route, no monitor of any kind: `registry`,
`cloudflare-ddns`, karakeep's `chrome` / `meilisearch` / `time-tagger`, and `n8n-runners`. That
last one executes every workflow's code.

---

## The seam that shapes this slice

Slices 1 and 2 moved *leaf apps*: self-contained workloads whose only couplings were a hostname, a
volume, and a database. The monitoring plane has none of that shape.

- `node-exporter` and `cadvisor` read **daniel-server's own kernel and Docker state**. They cannot
  observe daniel-server from inside daniel-box.
- `promtail` tails **daniel-server's log files** (`authlog`, `syslog`, `traefik`) and its Docker
  stdout streams.
- `monitor-bridge` reaches `kopia:51515`, `sonarr:8989`, `radarr:7878`, `prowlarr`, and
  `docker-proxy` over **Docker networks that only exist on daniel-server**.
- `uptime-kuma` + `autokuma` both read the Docker daemon through `docker-proxy:2375` — Kuma to
  answer "is this container running", AutoKuma to discover the labels that define the monitors.
  That proxy is unpublished by deliberate security decision (see the revision note), so this
  coupling is not stretchable across hosts.

daniel-server does not join the cluster until **slice 7**. So the slice splits along that seam:

| | Can move now | Must stay until slice 7 |
|---|---|---|
| **What** | Storage, query, dashboards | Collectors, the alert brain, Docker-facing reach-outs |
| **Which** | Prometheus TSDB, Loki, Grafana | node-exporter, cadvisor, promtail, monitor-bridge, **uptime-kuma, autokuma** |
| **How it connects** | — | Remote-write / remote-ship northbound to the cluster |

The test for which side a service lands on is **not** "is it stateful" or "is it infrastructure" —
it is **does it read something only daniel-server has**. Kuma looked like alerting (movable) and is
actually a Docker-daemon client (not movable). That the first draft put it on the wrong side is the
seam working, not the seam failing.

This is a tighter boundary than design §5's reworks-vs-ports split, and it corrects that doc in one
place: **`monitor-bridge` is listed as a "straight port" and it is not.** Its *code* ports cleanly
— it is stdlib Python driven entirely by env vars. Its *network position* does not: it currently
sits on five Docker networks to reach five different dependencies. Until slice 7 it stays on
daniel-server and gains northbound targets, and only its already-migrated dependencies (`n8n`) move
to cluster addressing — which slice 2 already did.

---

## Decisions

### D1 — Grow `claude-otel` into the cluster's observability stack; do not build a second one

The cluster already runs Prometheus, Grafana, Loki and Tempo with PVCs, retention and ingress.
Standing up a parallel "monitoring" namespace beside it would mean two Grafanas to log into, two
Prometheus TSDBs to size, and two sets of dashboards — for no gain.

Slice 3 widens `claude-otel` rather than duplicating it: extend its scrape config, add the Docker
stack's datasources and dashboards, and give it a remote-write receiver for daniel-server.

**Consequence to accept:** the role name stops matching its job. Rename it to something like
`observability` as part of this slice, or keep the name and document that it is no longer
Claude-specific. Renaming an Ansible role with a live PVC is not free — the PVC name is derived
from role defaults, so a rename must preserve `claim` names explicitly or it orphans the volumes.
Decide before the first commit, not after.

### D2 — AutoKuma **adds** the Files source alongside Docker, in the instance it already runs

AutoKuma supports three sources. From the upstream README, verbatim:

| Source | Support |
|---|---|
| Docker — container labels | ✅ |
| Files — `.json`/`.toml` | ✅ |
| Kubernetes — custom resources | ⚠️* |

> \*These sources are supported on an as-is basis as I'm currently not running any of them (they
> are basically looking for a maintainer, please get in contact if you'd like to adopt one...)

The Kubernetes provider is the obvious-looking choice and it is the wrong one: the author does not
run it and is asking for a maintainer. The Files provider is fully supported and maps cleanly onto
what this repo already does — `ansible/templates/autokuma.yml.j2` generates monitor definitions
from inventory, and it does not care whether the output is a label or a file.

**The two sources are independent toggles, not alternatives** — verified against the upstream
configuration reference (`autokuma.bigboot.dev/dev/autokuma/configuration/`):

| Variable | Purpose |
|---|---|
| `AUTOKUMA__DOCKER__ENABLED` | Docker label source — what we use today |
| `AUTOKUMA__FILES__ENABLED` | Files source on/off |
| `AUTOKUMA__STATIC_MONITORS` | Folder AutoKuma scans for `.json`/`.toml` definitions |

So **one AutoKuma instance serves both**, and the first draft's "run a second AutoKuma against a
second Kuma" is unnecessary. The docs do not state the defaults for either `ENABLED` flag, so set
both explicitly rather than inheriting an unknown.

**Authorship does not change; only the emission target for the k8s half.** The `kuma()` macro keeps
its signature and its Docker branch. A new file branch renders the same fields as JSON into the
mounted directory. Per the upstream static-monitors page, **the filename without its extension
becomes the AutoKuma id** — the same identity the label form spells as `kuma.<id>.`:

```json
{ "type": "http", "name": "Example", "url": "https://example.com", "interval": 60, "max_retries": 3 }
```

**Watch the field-name skew.** Our Docker labels use `maxretries` for the `docker`/`http`/`port`
branches but `max_retries` for `push`, and the documented file example uses `max_retries`. Confirm
against a real created monitor rather than assuming the label spelling carries over.

**Cross-host authorship is already precedented.** The file branch renders on daniel-server from
daniel-box's inventory, which is exactly what Docker Traefik already does for bridged routes —
`ansible/roles/containers/traefik/templates/config.yml.j2:6`:

```jinja
{% set bridged = hostvars['daniel-box'].containers_list | default([]) | selectattr('bridge_hostname', 'defined') | list %}
```

Same `hostvars['daniel-box']` read, same "one host's role renders from another host's inventory"
shape. No new plumbing to invent.

### D3 — Kuma stays on daniel-server; the `docker`-type monitors are why *(inverted from draft 1)*

`labels()` defaults to `monitor_type='docker'` and emits
`kuma.<id>.docker.docker_host={{ kuma_docker_host }}`, where `kuma_docker_host: 1` is the numeric
id of a **Docker Host entry configured inside Uptime Kuma's own database**. Kuma's docker monitor
then asks that daemon whether the container is running.

So most per-container monitors depend on Kuma being able to reach a Docker daemon. Moving Kuma into
the cluster does not remove that dependency — it stretches it across hosts.

The draft weighed two options — expose docker-proxy over TCP, or convert ~40 monitors to another
type. **The first is closed on security grounds** (see the revision note: reachability is the only
control preventing secret enumeration, and no proxy publishes a port). The second is ~40 monitors of
churn against a target that changes again at slice 7, when those containers stop being Docker
containers at all.

**Both options existed only to enable a Kuma move that this slice no longer needs.** Kuma stays
where it is. Its Docker coupling is collector-side, it sits on the stays-until-slice-7 side of the
seam, and every `docker`-type monitor keeps working unmodified because nothing about them changes.

What this slice *does* add is the Files source (D2) for the workloads that have **no** Docker
container to watch — the k8s pods. Those get `http`/`push` monitors, which need neither a Docker
daemon nor a metrics pipeline. The `docker`-type conversion question is deferred to each service's
own migration, which is where it belongs.

**Consequence worth stating:** slice 3 no longer migrates the alert brain. That removes the
slice's single largest continuity risk, and it is why the batch order below changes.

### D4 — *(deferred to slice 7)* Recreate Kuma's state declaratively; do not migrate the SQLite database

**Not part of slice 3 any more** — D3 keeps Kuma on daniel-server, so there is no state to move and
no Discord notification or Docker Host entry to re-create. Kept here because the analysis stays
correct and slice 7 will need it; the reasoning below is what makes a Kuma rebuild cheap whenever
it does happen.

Kuma's entire state is one bind mount (`./data:/app/data`). It holds monitors, notification
config, heartbeat history, the Docker Host entry, and the UI's own 2FA.

Recreate rather than copy, because **the 42 push tokens are client-supplied**. `monitor-bridge`
pushes to `KUMA_PUSH_<NAME>` values that come from `secrets.yml`, and Kuma honours whatever token
the client presents. A recreated Kuma with the same tokens accepts the same pushes with no change
to the bridge at all. That is the property that makes a declarative rebuild cheap here and it is
worth stating plainly, because it is not true of most stateful services.

What is lost: **heartbeat history**. Accept it, and say so — it is a graph of past uptime, not a
control. What must be re-created by hand: the Discord notification (its id is
`kuma_notification_id`, referenced by every monitor via the macro) and the Docker Host entry from
D3. Both are one-time UI actions, both are already documented as operator prerequisites in the
`uptime-kuma` role.

**Hazard, verbatim from slice 1 and applying unchanged:** do not run two Kuma instances against the
same storage backend. The new instance gets its own PVC.

### D5 — Port the Prometheus config; do not adopt `kube-prometheus-stack` in this slice

Design §5 calls `kube-prometheus-stack` "the idiomatic target, but adopting it is a config rewrite,
not a lift." Two findings make deferring it clearly right:

1. **There are no `rule_files`.** No recording rules, no alerting rules — the entire alerting path
   is `monitor-bridge` → Kuma push → Discord. `kube-prometheus-stack`'s main value is its bundled
   rules and Alertmanager wiring, and this homelab uses neither.
2. **The scrape config is eleven jobs of plain YAML.** Porting it is mechanical.

What the cluster genuinely needs on top is **`kubernetes_sd_configs`** for pod/node discovery —
that is what closes the seven-unmonitored-workloads gap, and it is a scrape-config addition, not a
framework adoption. Revisit `kube-prometheus-stack` at slice 7 when both nodes are in the cluster
and the node-exporter/cadvisor duplication actually needs resolving.

### D6 — Keep Grafana's datasource UIDs byte-identical

`provisioning/datasources.yml.j2` pins three UIDs: `EGdsQqhVk` (Prometheus), `bf4q19tuivta8e`
(Loki), `tempo`. Dashboards reference datasources by UID, and a prek hook
(`Validate Grafana dashboard datasource uids`) already enforces the correspondence.

So the migration lever is: **provision the cluster Grafana with the same three UIDs**, and every
existing dashboard works with no edit. Change them and every panel breaks at once. This is the
cheapest large win in the slice and the easiest to get wrong by letting Grafana auto-generate.

Note `claude-otel`'s Grafana already provisions its own datasources — reconciling those UIDs with
these three is the concrete first task of D1.

### D7 — Alerting must stay continuous while the alerting stack is what moves

This is the decision most likely to be left implicit, and this repo has been bitten twice in one
week by monitoring that read green while broken — the B2 transaction cap (2026-08-02, 9.5 h) and
the gitops-behind defer (2026-08-07, 11 h). A slice that migrates the alert brain must say what
watches the watchers during the window.

**Rescoped by D3, not dropped.** The alert brain no longer moves, so the parallel-Kuma machinery
the draft prescribed is gone — and with it the slice's largest continuity risk. But the *metrics
path* still moves, and that carries a continuity risk of its own with a nastier failure shape.

**The risky step is pointing `monitor-bridge` at a cluster Prometheus, not moving Kuma.**
`check.py:2896` defines `PROM_DEPENDENT` as exactly twelve checks — `disk`, `cert`, `memory`,
`restarts`, `oom`, `cpu`, `targets`, `traefik5xx`, `b2_trend`, `ups`, `janitorr`,
`promtail_dropped`. When the Prometheus Reachable gate reads down, all twelve are **suppressed**:
pushed `up` with a "skipped" message so their heartbeats stay alive.

That gate is doing exactly what it was built to do. But it means a cluster Prometheus that is
*reachable yet incompletely populated* is the worst case available: the gate passes, the twelve
checks run against a Prometheus missing their series, and each one reads whatever an empty query
result decodes to. Green, silent, wrong — the same shape as the B2 transaction cap (2026-08-02,
9.5 h) and the gitops-behind defer (2026-08-07, 11 h), twice in one week.

**Therefore: both Prometheus instances stay scrapeable, and `monitor-bridge` keeps pointing at the
Docker one, until the cluster Prometheus demonstrably serves every series those twelve checks
query.** Not "until it's up" — until the series exist. That is B2's exit test, and it is the one
gate in this slice worth being pedantic about.

What survives a monitoring outage regardless:

- the **off-box UptimeRobot dead-man** on the host itself,
- the **email backstop** attached to the Discord Delivery monitor (an independent SMTP path),
- `monitor-bridge`'s own container healthcheck and autoheal.

### D8 — Pod health comes from kube-state-metrics, and it needs two things that do not exist yet

The seven-workload gap is the reason cluster pod-health was scoped into this slice, and the table in
the revision note above is why it cannot be closed cheaply. Four of the seven expose no Service at
all, so their health is not an HTTP property — it is "does the Kubernetes API consider this pod
ready". Two ways to read that:

- **kube-state-metrics → cluster Prometheus → one `monitor-bridge` check.** One check covers all
  workloads, present and future, with no per-service wiring, and it is the repo's dominant idiom
  (42 checks already work this way).
- **An in-cluster CronJob pushing per-workload to Kuma.** Independent of the metrics pipeline, but
  it needs seven new push tokens, seven monitors, and readiness logic per workload — which is
  reimplementing kube-state-metrics by hand.

**Take the first.** But it has two prerequisites that are not in place, and both must be done before
the check is worth writing:

1. **kube-state-metrics is not deployed.** The cluster runs `metrics-server` (the resource-metrics
   API, for `kubectl top` and HPA) — that is a different thing and does not export
   `kube_pod_status_ready` or `kube_deployment_status_replicas_unavailable`.
2. **The cluster Prometheus is not reachable from daniel-server.** Its hostPort is pinned to
   loopback — live: `{"containerPort":9090,"hostIP":"127.0.0.1","hostPort":9090}`, set from
   `claude_otel_query_host_ip` — and it has no IngressRoute (only `grafana` does in that
   namespace). `monitor-bridge` runs on daniel-server, so it cannot query `10.43.39.218:9090`.

   **The precedent is `n8n-monitoring`**, an IngressRoute that exists for exactly this problem —
   monitor-bridge needing to reach a cluster service. It is narrowly scoped, and that scoping is
   the part to copy:

   ```
   match: Host(`n8n-k8s.local.daniel-hunter.com`) && (PathPrefix(`/api/v1/workflows`) || PathPrefix(`/api/v1/executions`))
   middlewares: [rate-limit]
   ```

   LAN-only (`.local.`), path-restricted to the two endpoints actually needed, rate-limited. A
   Prometheus route should be the same shape — not a blanket `:9090`.

**Open decision, resolve before writing the check — which Prometheus does it query?** `check.py`'s
single `prom_ok` gate currently describes one dependency. A `k8s_pods` check querying the *cluster*
Prometheus while the other twelve query the *Docker* one breaks that: the gate would be watching a
source the new check does not use. Two ways out, and one must be picked deliberately:

- Give the gate a **second arm** (cluster Prometheus reachable) with its own skip set, mirroring
  `B2_DEPENDENT`; or
- Have the check **fail closed on an absent series** rather than inheriting a gate that is not
  watching its source.

The failure mode being avoided is specific: an empty query result decoding to "0 unavailable
replicas → up". Green, silent, wrong — the same shape as the B2 transaction cap and the
gitops-behind defer. Whichever arm is chosen, add the disjointness test alongside the existing
`PROM_DEPENDENT` / `LOKI_DEPENDENT` / `B2_DEPENDENT` / `STARTUP_GRACE` guards, which exist precisely
so these sets cannot drift.

---

## Execution log

**B1 — done 2026-08-07** (`1e10ad12`). kube-state-metrics deployed to `observability`; Prometheus
gained a ServiceAccount + list/watch ClusterRole and a cadvisor job via the API-server proxy; a
query-only LAN-only IngressRoute (`prometheus-k8s.local.<domain>`, `PathPrefix(/api/v1/query)`)
makes it reachable from daniel-server. Verified: all four scrape targets up, all 18 `homelab`
deployments reporting, and **all seven previously-unmonitored workloads returning
`kube_deployment_status_replicas_unavailable` through the exact path monitor-bridge will use**.
`/graph` returns 404 through that route, as intended.

**B2 — done 2026-08-07** (`9f93cf7d`). One `monitor-bridge` check (`k8s_workloads`) reading
kube-state-metrics through the cluster Prometheus, plus its own `cluster_prometheus` reachability
gate and `CLUSTER_DEPENDENT` skip set. D8's open question resolved as approved — **both** arms,
because they cover different faults: the gate covers "cluster Prometheus unreachable" (a root
cause, correctly suppressed) and the check's series-count floor covers "cluster Prometheus fine but
kube-state-metrics not scraped" (which the gate structurally cannot see, and where suppression
would turn a blind monitor green).

Verified live in both directions: `OK k8s_workloads - 34 k8s workloads healthy`, and with the floor
forced above the live count, `DOWN … UNKNOWN, not OK`. Kuma monitors created, pushes landing.

**The rollout gate (`08ccbbd6`) was fixed first, and needed two attempts.** The first version
compared `readyReplicas` to `spec.replicas` and did *not* catch the bug it was written for —
re-breaking the probe deliberately showed the Deployment reading `desired=1 ready=1 updated=1`
while the pod sat at `READY 1/1 RESTARTS 3`. A crashloop that recovers between kills is invisible
to every readiness-derived field, so the gate now samples **restart counts** across the window.
Both directions tested.

**Unplanned: a live docker-proxy outage, found and fixed.** All four proxies had been
`Up 5 days (unhealthy)` for ~1.5 h — dockerd restarted at 14:55 UTC and replaced
`/var/run/docker.sock`, and the containers still bind-mounted the old inode (host `2834440` vs
container `1565`). AutoKuma had been 503ing throughout, so **no monitor changes had applied** —
including `monitor-bridge-b2-reachable` from earlier the same day, which had never actually been
created in Kuma despite the deploy reporting success. `autoheal` cannot recover this: a restart
does not re-mount, only a recreate does. Fixed by force-recreating all four. Written up as a
memory, since the class recurs on every dockerd restart.

Two things bit during the B1 kube-state-metrics work, both now encoded in the templates:

- **kube-state-metrics' probes use two different ports.** `/livez` is on the metrics port (8080),
  `/readyz` on the telemetry port (8081). Pointing liveness at 8081 returns 404, the kubelet
  restarts the container ~35 s after each start, and the result is a CrashLoopBackOff with a
  completely clean application log — the app logs a successful startup every time.
- **Pi-hole only emits a VIP record per `containers_list` entry that has a `hostname`.**
  `claude-otel`'s names Grafana, so `prometheus-k8s` fell through the `local.<domain>` wildcard to
  daniel-server and collected *that* Traefik's 404 — which reads like a broken IngressRoute rather
  than a missing DNS record. Added `extra_hostnames` for entries fronting several routed workloads.

**Also observed, not fixed (pre-existing, out of scope):** `--check` on this role fails at the
OTLP bind assert, because check mode skips the `command` task that reads the value and the assert
then compares against an empty string. Same class as the `seed_volume` check-mode failure. And the
role's `rollout status` gate did **not** catch the crashlooping kube-state-metrics: readiness
passed, the Deployment went Available, `rollout status` returned success, and only then did the
liveness probe start failing. A delayed liveness failure is invisible to that gate.

### B3 done — 2026-08-07 — northbound, and the collision it exposed

Remote-write, not cluster-side scraping. The deciding evidence was that **all eleven Docker scrape
jobs target a container name on the internal `monitoring` network and none is published to the
host** — scraping inward would mean exposing eleven jobs' worth of ports to the LAN, against one
outbound connection. Verified live: 11 jobs and 1326 distinct metric names now arrive in the
cluster Prometheus.

Three things were found by checking rather than assuming, and each changed the design:

- **`external_labels` is mandatory here, not hygiene.** Both instances genuinely produce
  `job="otel-collector", instance="otel-collector:8889"` for two *different* collectors, and the
  new self-scrape adds a second such pair on `job="prometheus", instance="localhost:9090"`.
  Unlabelled, the two sources collide into one series and the receiver rejects half the samples as
  out-of-order — corrupting both sides, not just one. Confirmed working afterwards: each pair now
  resolves to two distinct series separated by `origin`. External labels apply only on
  remote-write, so the Docker Prometheus still serves those series **unlabelled** locally (checked
  via `probe.py metric`) — which is what keeps this reversible rather than a cutover.
- **The write path is a second IngressRoute, not a widened query rule.** Folding `/api/v1/write`
  into the existing `PathPrefix('/api/v1/query')` rule would have made every LAN host able to
  inject metrics into the instance B5 makes authoritative. It is guarded by
  `ClientIP(k8s_bridge_client_ip/32)`, and the guard is real rather than decorative for a reason
  worth recording: the cluster Traefik Service is `externalTrafficPolicy=Local`, so the source IP
  is the true TCP peer. Under `Cluster`, kube-proxy would SNAT it to a node IP and the matcher
  would silently match nothing. Tested from daniel-box (not the permitted host): read route 200,
  write route **404** — no rule matched. No rate-limit on the write route, deliberately: a sender
  replaying a backlog is exactly what would trip a limiter, and remote-write treats 429s as
  failures, so limiting it converts a brief outage into permanent gaps.
- **The cluster Prometheus had no self-scrape at all.** `prometheus_tsdb_head_series` returned an
  empty vector. B5 promotes this instance to the one alerting reads from, so its own ingestion
  rate and footprint had to be answerable first. Added.

**Capacity — the configured 30 d retention is now aspirational, and B6 needs to know that.**
Post-B3 the cluster Prometheus carries 38 568 head series (up from 23 617) at ~1350 samples/s.
At a typical ~1.7 bytes/sample that is roughly 200 MB/day, so the new
`--storage.tsdb.retention.size=3GB` cap binds at **about 15 days**, well before
`retention.time=30d` does. This is an estimate, not a measurement — no blocks have been compacted
yet. It does **not** block B5: the longest lookback any monitor-bridge query uses is
`B2_TREND_WINDOW=7d`, comfortably inside 15 days. It does bear on **B6**, which retires a
Prometheus holding 90 d in favour of one holding ~15 d; raising the PVC is a B6 decision, taken
with a real measurement rather than this estimate.

The size cap itself is not optional. `retention.time` bounds how *old* a block may get, not how
much it holds, so doubling the ingest rate grows the footprint without aging anything out — and a
full Prometheus PVC wedges rather than degrades, with the instance that would alert on it being
the one that filled up.

**New monitor: `Prometheus Remote-Write Lag`.** Remote-write buffers, retries, then drops, all
without the sender's own health changing, and a stale cluster copy answers queries exactly like a
current one. B5's exit test could therefore pass at the moment it is run and be false an hour
later. An absent lag gauge reads as **DOWN**, for the same reason the k8s workload check treats
absent series as UNKNOWN: no queue at all is the total-failure case and the one most likely to
look like silence.

It has **three** arms, because they fail independently: lag (receiver down or queue stalled),
`samples_failed_total` (permanent rejection after retries), and `enqueue_retries_total`
(backpressure). The third is the arm lag structurally cannot cover — an overflowing queue discards
its *oldest* samples while the newest keep flowing, so `highest_sent_timestamp` advances normally
and the lag reads healthy while the cluster copy develops holes. Same lesson as the promtail M2
review: an alert scoped to one drop reason silently misses the others.

Deliberately **not** `prometheus_remote_storage_samples_dropped_total`, which is the obvious
metric to reach for: enumerating the sender's 34 `prometheus_remote_storage_*` series showed it
does not exist in this Prometheus build. An absent selector yields an empty vector that reads as
0 forever, so it would have been a dead arm indistinguishable from health — the exact failure the
check exists to prevent. `samples_failed_total` was confirmed present the same way rather than
assumed.

The check is also graced on the sender's own uptime, reading `process_start_time_seconds` rather
than joining `STARTUP_GRACE` (which must stay disjoint from `PROM_DEPENDENT`), and mirroring
janitorr's uptime gate. An unreadable uptime does **not** grace.

**The failure it guards against is narrower than it first appeared, and the live test is what
showed that.** The reasoning was that `queue_highest_sent_timestamp_seconds` is registered at 0
before the first successful send, so `time() - 0` ≈ 1.8e9 would page on the first cycle after
every `prometheus` deploy. Restarting the container to confirm produced
`remote-write current (79s behind...)` instead — no page. The reason is that a PromQL instant
query looks back five minutes, so it serves the *pre-restart* gauge value until the restarted
Prometheus scrapes itself again, and remote-write has normally sent by then. The zero-gauge case
is therefore only reachable when the receiver is *also* unavailable at sender startup, which is
precisely when a bounded grace is wanted. The gate stays, its justification is narrower than
claimed, and the unit tests cover the logic directly.

Live, all three arms: `remote-write current (46s behind, 0 lost + 0 overflows in 1h)`.

**Found while here, and the sharpest B5 landmine: after the repoint, unqualified queries silently
span both estates.** The cluster Prometheus holds daniel-server's series alongside its own, so
every monitor-bridge query that does not pin `origin` widens the moment B5 repoints it:

- `container_*` is the big one. Docker's `cadvisor` job and the cluster's `kubernetes-cadvisor`
  job both produce it, so `check_restarts`, `check_oom` and `check_cpu` would start covering k8s
  pods and naming them as offenders — plausible-looking output from a check that silently changed
  scope.
- `check_targets_down`'s bare `up` gains the cluster's own scrape targets, and `down_exporters`
  matches on `job`, so an `EXPORTER_DEPENDENT` name would match either estate's job of that name.

None of these is *wrong* — arguably some are improvements — but every one is a scope change
arriving by accident. **B5 must decide per query whether it wants one estate or both**, and the
decision belongs in the query (`origin="daniel-server"` where the old semantics are intended),
not in a note. This is the whole reason `origin` exists as a label rather than the series being
merged.

The way this was found is worth keeping: reading `prometheus_tsdb_head_series` post-B3 returned
**two** series, and taking `result[0]` gave a number that could have been either estate's. The
committed figures above were re-derived with an explicit `{origin=""}` (cluster-native: 38 573
series, 1341.6 samples/s) rather than left to result ordering. The same ambiguity is exactly what
bites the checks above.

### B4 done — 2026-08-07 — sixteen dashboards, no dashboard edited

**Aliased the datasources rather than renaming them.** D6's lever is that a matching UID makes a
ported board work untouched, and every one of the sixteen references its datasource by UID
(checked: all 16 bare-string refs in the set are the Prometheus UID `EGdsQqhVk`, and not one board
references a datasource by *name*). But retagging the cluster's `Prometheus` from uid `prometheus`
to `EGdsQqhVk` would have broken this cluster's own Claude Code board, which uses the current uids
in 24 panels. So the two Docker uids were added as **additional** datasources over the same
backends — `Prometheus (daniel-server uid)`/`EGdsQqhVk` and `loki`/`bf4q19tuivta8e`. Tempo already
shared the uid `tempo`. Both dashboard sets now load unmodified, which is exactly what B4 asks to
prove. Confirmed in the provisioning log:
`inserting datasource from configuration name="Prometheus (daniel-server uid)" uid=EGdsQqhVk`.

Two datasources over one backend also turns out to be *correct* rather than merely convenient:
`timeInterval: "1m"` is load-bearing on the daniel-server alias (Grafana derives
`$__rate_interval` from it, and at the 15s default every `rate()` panel over 1m-scraped data
returns empty), while the cluster's own series are scraped at 15s and want it unset. One shared
datasource could not have been right for both estates now that they coexist in one TSDB.

**`AI/claude-code.json` was deliberately not ported.** Its dashboard uid `claude-code-otel` is the
same uid this cluster's vendored Claude Code board already claims, and the two have diverged (25
datasource refs vs 12). Loading both would let the provisioner resolve the collision by
last-writer-wins, silently, on a 30s timer. Reconciling them is content work with no test, and
doing it here would contradict B4's own criterion that no dashboard was edited. Sixteen ported,
one deferred.

**The real delivery constraint was not the one expected.** The plan assumed the 1 MiB ConfigMap
cap; the deploy failed on a different limit:

```
The ConfigMap "grafana-dashboards-infrastructure" is invalid:
metadata.annotations: Too long: may not be more than 262144 bytes
```

That is the `kubectl.kubernetes.io/last-applied-configuration` annotation that **client-side**
`kubectl apply` writes — a 256 KiB cap, and splitting folders further cannot fix it because
`node-exporter-full.json` is 442 KB and would breach it alone. Server-side apply does not write
that annotation at all. The dashboard ConfigMaps therefore get their own directory and their own
`kubectl apply --server-side --force-conflicts`, scoped to them; every other manifest in the repo
is small and client-side apply remains fine. `--force-conflicts` is needed because the four
folders small enough to succeed on the first attempt are already owned by the client-side field
manager.

The JSON is **not** copied into the k8s role. `roles/containers/grafana/files/dashboards` stays
the single source of truth and both Grafanas mount the same files — a second copy is precisely how
the two Claude Code boards diverged.

**Proof — which panels render empty.** Rather than clicking through the UI, every metric name
referenced by the sixteen boards was checked against the cluster Prometheus (1474 distinct names).
13 of 16 are fully covered. The three with gaps reference 7 distinct missing metrics:

| Dashboard | Missing |
|---|---|
| `Infrastructure/node-exporter-full.json` (216/220) | `node_hwmon_temp_crit_hyst_celsius`, `node_netstat_Tcp_MaxConn`, `node_pressure_irq_stalled_seconds_total`, plus `chip_name` (a label, not a metric — a false positive of the extractor) |
| `Security/crowdsec-*.json` | `cs_bucket_created_total`, `cs_cloudwatch_stream_hits_total`, `cs_journalctlsource_hits_total`, `cs_syslogsource_hits_total` |

**None of these is a migration gap.** Querying all eight names against daniel-server's own
Prometheus returns `no data` — they are absent there too, so those panels are equally empty on the
Grafana they came from. They are unconfigured CrowdSec acquisition sources (journald, syslog,
cloudwatch) and hardware/kernel counters this host does not expose. Nothing was lost in the port.

Two honest limits on that proof: the extractor reads PromQL `expr` fields only, so the two
Loki-backed boards (`Logs/logs.json`, `Infrastructure/alert-history.json`) report 0/0 and are
**not** validated by it; and folder placement follows from `foldersFromFilesStructure: true` and a
clean provisioning run (`starting` → `finished to provision dashboards`, no errors) rather than
from looking at the UI, which needs credentials this session does not hold.

---

## Batches — vertical, each independently exercisable

The default shape for this slice is horizontal (Prometheus → Loki → Grafana → Kuma → AutoKuma), and
it is wrong: nothing is checkable until the end. Sequenced so each batch ends in something
observable.

**Order note.** Gap closure stays *after* cluster scraping, where the first draft had it. A
mid-session attempt to move it to the front was reverted on the reachability evidence in the
revision note — the seven workloads have no endpoint to probe, so their health is only readable via
kube-state-metrics (D8), which is downstream of B1.

### B1 — One cluster metric, end to end, and a reachable cluster Prometheus

Add `kubernetes_sd_configs` to the cluster Prometheus, deploy **kube-state-metrics** (D8), and
scrape **one** workload. Expose the cluster Prometheus to daniel-server via a narrow LAN-only
IngressRoute on the `n8n-monitoring` pattern — path-restricted and rate-limited, not a blanket
`:9090`. Point the existing cluster Grafana at it with the D6 UIDs.

**Prove it:** a panel in the cluster Grafana shows that pod's memory, **and** `curl` from
daniel-server returns a non-empty `kube_pod_status_ready` series. The second half is the one that
unblocks everything downstream; the loopback-pinned hostPort means it is currently impossible.

Nothing else changes; the Docker stack is untouched and still authoritative.

### B2 — Close the seven-workload gap

The moment B1 lands, this is unblocked and it is the highest-value item in the slice — it closes a
live coverage hole, not a migration step. Add one `monitor-bridge` check reading kube-state-metrics
for unready pods / unavailable replicas across the cluster, resolving D8's open gate question first.

**Prove it:** `kubectl delete pod` on `n8n-runners` — the one that executes every workflow's code —
produces a Discord alert. Then confirm the check reports **down**, not up, when its series is
absent entirely (delete kube-state-metrics briefly): an empty result must not decode to healthy.

### B3 — Northbound from daniel-server

Configure remote-write from the Docker Prometheus to the cluster Prometheus (or a scrape of
daniel-server's exporters from the cluster — decide by which survives slice 7 better).

**Prove it:** a `node_*` series from daniel-server is queryable in the cluster Prometheus, and the
existing Docker Grafana is still serving the same series. Both stacks now see the same data, which
is the property that makes the rest of the slice reversible.

**Do not repoint `monitor-bridge` here.** See B4.

### B4 — Dashboards onto the cluster Grafana

Provision the cluster Grafana with the three pinned UIDs from D6 and load the existing dashboards
unmodified.

**Prove it:** every dashboard renders against cluster-side data with **no dashboard edits**. A panel
that renders empty is a missing series, which is exactly the signal B4 needs — so record which
panels are empty rather than fixing them here.

### B5 — Repoint `monitor-bridge`, behind an explicit series check

The slice's one genuinely risky step (D7). Before changing `PROM_URL`, confirm the cluster
Prometheus actually returns a non-empty result for the query behind **each of the twelve
`PROM_DEPENDENT` checks**. Reachable is not sufficient; populated is the bar.

**Prove it:** run `check.py --once` against the cluster Prometheus and diff its 42-line output
against the same run on the Docker Prometheus. Identical verdicts, no check newly reporting
"skipped". Any divergence is a missing series, not a passing test.

### B6 — Retire the Docker query layer

Stop the Docker `prometheus` and `grafana`. **`uptime-kuma`, `autokuma`, `node-exporter`,
`cadvisor`, `promtail`, `monitor-bridge` and `autofix-bridge` all keep running** — they are the
slice-7 residue by design, and per D3 the first two are now part of it.

---

## Hazards

**Loki has no `containers_list` entry.** It is deployed from inside the `grafana` role's compose,
alongside `promtail`. So "migrate Grafana" silently means "migrate Loki and promtail too" unless
the seam in D-above is applied: Loki (storage/query) moves, promtail (collector) stays. Splitting a
role that currently ships three services is the concrete work, and `--tags grafana` will not mean
what it used to.

**Kuma needs `NET_RAW` — *deferred to slice 7 with the Kuma move*.** The Docker compose grants it
explicitly so ping monitors (`daniel-pi-host`) can open raw ICMP sockets. A Pod needs the same
capability added, and the cluster's default `securityContext` drops ALL. A ping monitor that can
never succeed looks like a down host. Recorded here so slice 7 does not rediscover it.

**The Files source must not disturb the Docker source.** AutoKuma reconciles monitors against its
own DB (`AUTOKUMA__DB_PATH=/data/autokuma.db`). Enabling a second source on a live instance that
already owns ~40 monitors is the one step in B0 that can do damage — a static file colliding with
an existing Docker-derived id would have AutoKuma reconcile one against the other. Namespace the
generated filenames (the filename *is* the id) so no k8s monitor can collide with a container name.

**The self-monitoring recursion.** The cluster Prometheus scraping itself, and the cluster Kuma
monitoring the cluster Prometheus, means a single Longhorn or node failure takes out both the
service and its watcher. `claude-otel` already has a `telemetry-health.sh` cron for exactly this
reason — extend it rather than rediscovering the need.

**Two Grafanas during the slice is correct, not a mistake.** Resist consolidating early; the
parallel period is what makes B2 reversible.

---

## Exit criteria

- [ ] kube-state-metrics is deployed and the cluster Prometheus is reachable from daniel-server via
      a path-scoped, LAN-only IngressRoute (D8)
- [ ] The seven previously-unmonitored k8s workloads are covered by a check that fires on pod
      deletion — **and reports down, not up, when its series is absent**
- [ ] A daniel-server metric and a cluster-pod metric are both queryable from the cluster Prometheus
- [ ] Every dashboard renders in the cluster Grafana **with no dashboard edits** (D6 held)
- [ ] All twelve `PROM_DEPENDENT` queries return non-empty against the cluster Prometheus
- [ ] `check.py --once` gives identical verdicts against both Prometheus instances
- [ ] The Docker `prometheus` + `grafana` are stopped for 1 h with no loss of alerting
- [ ] `monitor-bridge`'s check logic is unchanged apart from the one new pod-health check

**Explicitly *not* in this slice** (moved to slice 7 by D3): Kuma or AutoKuma running in the
cluster, Kuma state recreation, the Discord notification and Docker Host re-creation, and any
conversion of `docker`-type monitors.

**Not required by this slice, though verified as available** (D2): the AutoKuma Files source. It
turned out not to be what closes the coverage gap — D8 does — so enabling it is optional here and
naturally belongs with the slice-7 Kuma move.

---

## Unverified — resolve during execution, not by assuming

### Resolved 2026-08-07, before execution

- **Whether the cluster can reach daniel-server's docker-proxy** (D3) — **RESOLVED: it cannot, and
  must not.** No docker-proxy publishes a host port (empty `PORTS` for all four of `docker-proxy`,
  `docker-proxy-portainer`, `docker-proxy-codeserver`, `docker-proxy-lifecycle`), and
  `host_vars/daniel-server.yml:71-75` documents reachability as the *only* control preventing
  `GET /containers/{id}/json` from enumerating every container's secrets. This inverted D3 and
  reshaped the slice.
- **PVC naming across the `claude-otel` rename** (D1) — **RESOLVED: safe.** Claim names are
  literal in the templates, not role-derived (`name: prometheus-data` at `prometheus.yaml.j2:27`,
  `claimName: prometheus-data` at `:97`). Live PVCs `grafana-data` (1Gi), `loki-data` (10Gi),
  `prometheus-data` (5Gi), `tempo-data` (5Gi) are all `longhorn-nobackup` and Bound. A role rename
  does not touch them.
- **Whether AutoKuma can run Docker and Files sources at once** (D2) — **RESOLVED: yes**, they are
  independent toggles (`AUTOKUMA__DOCKER__ENABLED` / `AUTOKUMA__FILES__ENABLED` +
  `AUTOKUMA__STATIC_MONITORS`). This removed the draft's second-AutoKuma-instance requirement.

- **Northbound = remote-write, resolved in B3 by measurement.** Not the "survives slice 7 better"
  argument the draft expected to decide it: none of the eleven Docker scrape targets is published
  to the host, so cluster-side scraping meant exposing eleven jobs' worth of ports to the LAN
  against one outbound connection. See the B3 execution log.

### Still open

- **Static-file coverage of `docker`-type monitors** (D2). Now largely moot — D3 keeps every
  `docker`-type monitor on the label path, and B0 only needs `http`/`push`. Becomes a slice-7
  question.
- **Exact field-name spelling in static files** (D2). Our labels use `maxretries` for
  `docker`/`http`/`port` but `max_retries` for `push`; the documented file example uses
  `max_retries`. Settle it against a created monitor in B0, not by reading the macro.
- **Default values of the two `ENABLED` flags** (D2). The upstream config page lists the variables
  but not their defaults. Set both explicitly rather than inheriting an unknown.
- **Which Prometheus the pod-health check queries, and how its gate is scoped** (D8). The open
  decision with the sharpest failure mode in the slice — an absent series must not decode to
  healthy. Resolve before writing the check, not after.
- **How narrowly the cluster Prometheus IngressRoute can be path-scoped** (D8). `n8n-monitoring`
  restricts to two `PathPrefix`es; the equivalent for Prometheus is probably `/api/v1/query`, but
  confirm what `check.py`'s `prom_scalar`/`prom_vector` actually call before locking the rule down.
