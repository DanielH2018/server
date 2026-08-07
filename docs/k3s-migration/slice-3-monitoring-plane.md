# k3s Slice 3 — The Monitoring Plane

**Status:** plan, not yet executed. Written 2026-08-07.

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

daniel-server does not join the cluster until **slice 7**. So the slice splits along that seam:

| | Can move now | Must stay until slice 7 |
|---|---|---|
| **What** | Storage, query, dashboards, alerting | Collectors and Docker-facing reach-outs |
| **Which** | Prometheus TSDB, Loki, Grafana, Kuma, AutoKuma | node-exporter, cadvisor, promtail, monitor-bridge |
| **How it connects** | — | Remote-write / remote-ship northbound to the cluster |

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

### D2 — AutoKuma moves to the **Files** provider, not the Kubernetes CRD provider

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

**The rework is therefore a change of emission target, not of authorship.** The `kuma()` macro
keeps its signature; the Docker branch emits `kuma.<id>.<type>.<field>=<value>` labels, and a new
file branch emits the same fields as TOML/JSON into a mounted directory. Both stacks can run at
once during the migration, which is what makes the cutover reversible.

**To verify before building on it:** that all four monitor types the macro emits (`docker`, `push`,
`port`, `http`) are expressible as static files. The file format is a generic `type = "..."` plus
the type's fields, and the label form is the same entity model with a different transport, so this
is expected to hold — but the static-monitors page does not state it explicitly, and `docker`-type
is the one worth actually testing (see D3).

### D3 — The `docker`-type monitors are the largest hidden item, and they gate where Kuma runs

`labels()` defaults to `monitor_type='docker'` and emits
`kuma.<id>.docker.docker_host={{ kuma_docker_host }}`, where `kuma_docker_host: 1` is the numeric
id of a **Docker Host entry configured inside Uptime Kuma's own database**. Kuma's docker monitor
then asks that daemon whether the container is running.

So most per-container monitors depend on Kuma being able to reach a Docker daemon. Moving Kuma into
the cluster does not remove that dependency — it stretches it across hosts.

Two options, and this needs deciding before the Kuma move, not during:

- **Keep the Docker Host entry, point it at daniel-server's docker-proxy over TCP.** Smallest
  change; every `docker`-type monitor keeps working unmodified. Cost: the cluster Kuma now depends
  on a daniel-server service, and the docker-proxy must be reachable from the cluster network.
- **Convert Docker monitors to another type.** Larger change (~40 monitors), but it removes the
  cross-host dependency and is the shape those monitors need after slice 7 anyway, when the
  containers stop being Docker containers at all.

**Recommendation: keep the Docker Host entry for slice 3.** The conversion is really part of each
service's own migration — a container that becomes a Pod needs a k8s-shaped monitor, and inventing
that mapping now, for services that have not moved, is work done against a target that will change.

### D4 — Recreate Kuma's state declaratively; do not migrate the SQLite database

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

What survives a Kuma outage:

- the **off-box UptimeRobot dead-man** on the host itself,
- the **email backstop** attached to the Discord Delivery monitor (an independent SMTP path),
- `monitor-bridge`'s own container healthcheck and autoheal.

What does **not**: everything else. Every threshold alert in the homelab is a Kuma push monitor.

**Therefore: both Kuma instances run in parallel for the whole slice, and the Docker one stays
authoritative until the cluster one has demonstrated a full alert round-trip.** The bridge can push
to both — the tokens are client-supplied and pushing twice costs one extra HTTP call per check.
Cut over only after B4 below passes.

---

## Batches — vertical, each independently exercisable

The default shape for this slice is horizontal (Prometheus → Loki → Grafana → Kuma → AutoKuma), and
it is wrong: nothing is checkable until the end. Sequenced so each batch ends in something
observable.

### B1 — One cluster metric, end to end

Add `kubernetes_sd_configs` to the cluster Prometheus and scrape **one** workload. Point the
existing cluster Grafana at it with the D6 UIDs.

**Prove it:** a panel in the cluster Grafana shows that pod's memory. Nothing else changes; the
Docker stack is untouched and still authoritative.

### B2 — Northbound from daniel-server

Configure remote-write from the Docker Prometheus to the cluster Prometheus (or a scrape of
daniel-server's exporters from the cluster — decide by which survives slice 7 better).

**Prove it:** a `node_*` series from daniel-server is queryable in the cluster Prometheus, and the
existing Docker Grafana is still serving the same series. Both stacks now see the same data, which
is the property that makes the rest of the slice reversible.

### B3 — AutoKuma emits files

Add the file-emitting branch to `autokuma.yml.j2`. Run a **second** AutoKuma against the **second**
Kuma, sourcing from files only, generating a small subset — the push monitors, which have no Docker
dependency.

**Prove it:** the cluster Kuma shows those monitors, and `monitor-bridge` — pushing to both — turns
them green. This is where the D2 assumption about type coverage gets tested for real.

### B4 — One full alert round-trip, then cut over

Widen the file-sourced set to all monitors, re-create the Discord notification and the Docker Host
entry (D3, D4).

**Prove it, and this is the slice's real exit criterion:** stop a container on daniel-server and
confirm a Discord message arrives *from the cluster Kuma*. Then stop the Docker Kuma for an hour
and confirm nothing goes dark.

### B5 — Close the seven-workload gap

With cluster scraping live, add pod-health monitors for the workloads that have never had one:
`registry`, `cloudflare-ddns`, karakeep's `chrome`/`meilisearch`/`time-tagger`, `n8n-runners`.

**Prove it:** `kubectl delete pod` on `n8n-runners` produces an alert. This is the gap that
motivated scoping the cluster pod-health alert into slice 3 in the first place.

### B6 — Retire the Docker query layer

Stop the Docker `prometheus`, `grafana` and `uptime-kuma`. Leave `node-exporter`, `cadvisor`,
`promtail`, `monitor-bridge` and `autofix-bridge` running — they are the slice-7 residue by design.

---

## Hazards

**Loki has no `containers_list` entry.** It is deployed from inside the `grafana` role's compose,
alongside `promtail`. So "migrate Grafana" silently means "migrate Loki and promtail too" unless
the seam in D-above is applied: Loki (storage/query) moves, promtail (collector) stays. Splitting a
role that currently ships three services is the concrete work, and `--tags grafana` will not mean
what it used to.

**Kuma needs `NET_RAW`.** The Docker compose grants it explicitly so ping monitors
(`daniel-pi-host`) can open raw ICMP sockets. A Pod needs the same capability added, and the
cluster's default `securityContext` drops ALL. A ping monitor that can never succeed looks like a
down host.

**The self-monitoring recursion.** The cluster Prometheus scraping itself, and the cluster Kuma
monitoring the cluster Prometheus, means a single Longhorn or node failure takes out both the
service and its watcher. `claude-otel` already has a `telemetry-health.sh` cron for exactly this
reason — extend it rather than rediscovering the need.

**Two Grafanas during the slice is correct, not a mistake.** Resist consolidating early; the
parallel period is what makes B2 reversible.

---

## Exit criteria

- [ ] A daniel-server metric and a cluster-pod metric are both queryable from the cluster Prometheus
- [ ] Every dashboard renders in the cluster Grafana **with no dashboard edits** (D6 held)
- [ ] Every monitor that exists in the Docker Kuma exists in the cluster Kuma, generated from files
- [ ] Stopping a container produces a Discord alert originating from the cluster Kuma
- [ ] The Docker Kuma is stopped for 1 h with no loss of alerting
- [ ] The seven previously-unmonitored k8s workloads each have a monitor that fires on pod deletion
- [ ] `monitor-bridge` is unchanged except for its northbound targets — no check logic edited

---

## Unverified — resolve during execution, not by assuming

- **Static-file coverage of `docker`-type monitors** (D2). Expected to work; not confirmed by the
  upstream static-monitors page. B3 tests it against push monitors first, which is the cheap half.
- **Whether the cluster can reach daniel-server's docker-proxy** (D3). Nothing has needed that path
  yet, and the same-bridge hairpin-NAT constraint from slice 2 means the reverse direction has
  already surprised us once.
- **Whether remote-write or cluster-side scraping is the better northbound** (B2). Remote-write
  survives daniel-server joining the cluster more gracefully; scraping is simpler now.
- **PVC naming across the `claude-otel` rename** (D1). Verify the claim names are preserved before
  the first apply, or the existing telemetry volumes orphan.
