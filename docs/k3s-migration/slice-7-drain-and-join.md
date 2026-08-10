# Slice 7 — Drain daniel-server, join it, name its residual role

> design.md row: *"Drain Docker on daniel-server; decide its residual role — exit: only
> `peanut` + DaemonSets remain, or the UPS moves too."* Slice-0 sharpened the order: the node
> joins the cluster **after** its Docker workload has drained, because Docker's iptables
> chains and hairpin NAT on a loaded host are the riskiest possible k3s starting point, and
> Longhorn's raise to 2 replicas at join is the step that makes failover exist.

Status: **PLANNED 2026-08-09** — authored while slice 6 awaits its router flip. Phase A gates
this slice on slice 6 actually closing; nothing here starts before that.

## Baseline — measured 2026-08-09 evening

- **26 Docker entries remain on daniel-server** (`containers_list`): traefik, authelia,
  portainer, docker-proxy, homepage, autoheal, watchtower, peanut, pihole, kopia, wg-easy,
  code-server, prometheus, grafana (carries loki), tempo, otel-collector, glances,
  uptime-kuma, scrutiny, homelab-mcp, monitor-bridge, autofix-bridge, bento-pdf, ical-proxy,
  terraria, terraria-stats. Running containers add the un-inventoried sidecars: autokuma,
  cadvisor, node-exporter, promtail, scrutiny-collector, scrutiny-influxdb, unbound, the
  demoted crowdsec agent, and three per-consumer docker-proxies (codeserver, lifecycle,
  portainer).
- **bento-pdf runs twice** — a Docker copy on daniel-server AND the slice-1 k8s canary. The
  Docker copy was never retired; the reverse bridge (slice-6 B4) fronts the Docker one.
  Reconcile in Phase C — likely a one-day cutover, since the k8s twin has run since slice 1.
- **The cluster already runs a full telemetry stack** (`claude-otel` namespace: Grafana,
  Prometheus, Loki, Tempo, collector, kube-state-metrics) plus the homelab-namespace
  Prometheus from slice 3. The Docker monitoring plane's remaining scope is the shrinking
  Docker fleet plus host-level telemetry — it consolidates, it does not port.
- **Docker Pi-hole drain** (slice-6 B3) is down to daniel-server's own containers — Docker's
  embedded DNS re-reads resolv.conf only at container recreate, so every Phase B/C retire
  shrinks it for free.
- **Four `*_host` flags** still anchor to daniel-server: `monitoring_controller_host` (Kuma
  pushes), `backup_controller_host` (kopia's Pi-peer scope), `portainer_manager_host` (the
  Pi's agent firewall), `renovate_notify_host` (arbitrary). Each flips or dissolves with its
  anchor, per slice-6 D5.

## Decisions

### D1 — daniel-server joins as an AGENT, not a second server

A two-member etcd is worse than one: quorum = 2, so *either* node going down halts the
control plane — availability strictly degrades. The single-control-plane asymmetry was
accepted in design.md §9; a second *server* only makes sense with a third member
(design decision 5 left that open). Agent join keeps the API on daniel-box, adds scheduling
capacity and a Longhorn replica target, and is reversible with `k3s-agent-uninstall.sh`.

### D2 — Drain-then-join stands (slice-0's order), with one refinement

Docker is not fully gone at join time: wg-easy and peanut (D6/design decision 3) stay Docker
indefinitely, and kopia stays a host service. So the join precondition is not "Docker absent"
but "Docker reduced to the residual set, with no published ports the cluster's dataplane
needs to own and no container on the `proxy` network". The riskiest interplay — Traefik's
published 80/443, the DOCKER-USER chain, hairpin NAT against the VIP — is gone once Phase E
retires the Docker edge.

### D3 — The Docker monitoring plane CONSOLIDATES into the cluster stacks

No port of prometheus/grafana/loki/tempo. Host-level telemetry (node-exporter, cadvisor for
the residual Docker set, promtail for host logs, SMART) re-targets the cluster: exporters
become DaemonSets or stay as host services scraped over the LAN; dashboards that still
matter move into the existing Grafana-ConfigMap mechanism; alert rules land beside the
slice-3 rules. The Docker Grafana/Prometheus/Loki/Tempo retire when their last consumer
does. Uptime Kuma + AutoKuma migrate (D4) — they are the alerting spine, not just a
dashboard. What survives of `otel-collector` is decided by its ONE hard constraint: Claude
Code publishes OTLP to host loopback, so the cluster replacement needs a hostPort/hostNetwork
listener on each node a session can run on (both).

### D4 — AutoKuma's label-driven monitor generation reworks onto the k8s side

AutoKuma reads Docker labels; the k8s workloads' monitors are already hand-declared in the
uptime-kuma compose template (the slice-2+ pattern). When Uptime Kuma itself moves to the
cluster, those declarations move from compose labels to AutoKuma's static-monitor files (its
docker-less mode), rendered from the same inventory. The docker-type container monitors die
with the Docker fleet — by then everything is probe-, push-, dns- or http-monitored.
`monitoring_controller_host` flips to daniel-box when Kuma moves; the Pi's push monitors and
the daniel-box disk push follow the URL, not the host.

### D5 — Portainer retires now, and takes its cohort with it (design decision 4, executed)

Replaced by nothing: `kubectl`+k9s serve the cluster; the residual Docker set is small enough
for `docker ps`. Retires together: portainer, docker-proxy-portainer, the Pi's
portainer-agent + its DOCKER-USER rule, `portainer_manager_host`, and homepage's portainer
widget (coupled per design §5).

### D6 — The residual role, named

daniel-server keeps, indefinitely: **peanut/NUT** (UPS is cabled there), **wg-easy** (the
remote-access lifeline; 51820 stays forwarded to .161 — moving it buys nothing and risks the
tool this migration is operated with), **kopia** (shrunk: host paths + Pi peer configs — it
backs up hosts, not workloads), the **k3s agent**, and node-level exporters. Everything else
drains. `docker` itself stays installed for that residual trio — has_docker stays true.

### D7 — homelab-mcp and otel-collector move LAST, after the join proves stable

Both are instruments used to operate the migration (design §5 "don't remove your own
instruments"). homelab-mcp additionally needs a redesign — its tools read the Docker daemon,
which by then describes only the residual set; its k8s successor reads the cluster API.
Deferred to Phase G, possibly beyond the slice.

**D7 SPLIT AND PARTIALLY EXECUTED EARLY (2026-08-10), operator-approved:** what gated the
Docker Loki retirement (D.2 step 4) was never "collector runs in the cluster" — it was
"the Claude stream stops landing in Docker stores". So the *exporter seam* moved now and
the *receiver* stays: the Docker otel-collector became a pure forwarder chaining all three
pipelines to the cluster claude-otel collector over a new write-only, ClientIP(.161/32)-
gated, h2c ingest IngressRoute (`claude-otel-ingest-k8s`; the loopback posture protects
READ access, and no read path was added). `localhost:4317` stays the client contract on
both hosts — zero settings.json divergence — and the forwarder + route dissolve at the
Phase F join exactly as this decision planned. Executed with it: Docker tempo RETIRED
(16 → 15; the forwarder was its only feeder — cluster tempo is the successor), the Docker
prometheus `otel-collector` job dropped, the AI/claude-code board re-homed to the
claude-otel role (its only consumer now), and the Tempo datasource + trace cross-links
removed from the Docker Grafana. homelab-mcp's `claude_code_usage`/`claude_code_events`
tools are dark for post-D7 data until the Phase G redesign (pre-D7 history stays in the
Docker prometheus TSDB). The homelab-mcp half of D7 remains deferred to Phase G.

## Steps

### A — Slice-6 close-out gate (blocks everything)

Router 80/443 → VIP flipped and LTE-verified; soak green (Kuma, CrowdSec seeing internet
noise, Authelia sessions on both stacks); the forward-bridge teardown landed
(`bridge_hostname` keys gone, `test_strangler_bridge.py` reworked to end-state); B2 backups
flowing again post-cap with the frozen restore points pruned. Docker Pi-hole retired once
its query log flatlines — its recreate-driven tail ends as Phases B/C retire containers.

**FLIPPED 2026-08-10 ~17:55 UTC (operator).** Verified from the cluster edge's own
metrics: the reverse-bridge router carries live traffic (it only matches public arrivals
for Docker-hosted names), public apps serve through the k8s Traefik, and no monitor
paged across the flip. LTE-verified by the operator ~18:00 UTC the same day. Still open
before the gate clears: a soak window, the forward-bridge teardown, and tonight's B2
re-arm.

### B — Dissolves and cheap retires — EXECUTED 2026-08-09 (ahead of Phase A, deliberately:
none of these depend on the slice-6 close-out)

Done, one commit each: **watchtower** (`35c0dc8d` — Renovate owns updates), **glances**
(`7e6f4453` — node-exporter/cAdvisor already scraped; homepage's whole glances-widget
Monitoring section went with it), the **Portainer cohort** (`83532b8e`, `580da2dd` — server +
docker-proxy-portainer, homepage widget AND homepage's proxy-net membership which existed
only for it, the Pi's agent + DOCKER-USER firewall torn down via a one-shot play,
`portainer_manager_host`, the Kuma port monitor, the Renovate lockstep rule + its CI test,
and both dead SOPS secrets). daniel-server: 26 → 23 entries. Still Phase B's tail:
docker-proxy-lifecycle once its consumers (autoheal, formerly watchtower) resolve, and
autoheal itself, which stays until the last health-checked Docker service drains.

### C — Remaining straight ports

bento-pdf (reconcile the duplicate — retire the Docker copy, repoint the reverse bridge's
host to the cluster route), homepage (its widgets re-target cluster names/VIPs; portainer
widget already gone), code-server, ical-proxy, terraria-stats (reads Loki — re-point at the
cluster Loki), monitor-bridge and autofix-bridge (their check targets are mostly cluster
services already; what remains Docker-side shrinks to host checks), healthchecks-style
verification per service as in slices 2-5.

**Executed so far:** bento-pdf and ical-proxy 2026-08-10 (`6baaf482`, `a762e35c`).
code-server 2026-08-10 (`c486f05b`) — ported WITHOUT its docker plumbing (operator
decision): no DOCKER_HOST, in-IDE docker CLI and devcontainers gone,
docker-proxy-codeserver + the `codeserver` net dissolved (`has_code_server: false`).
daniel-server: 20 → 19. Deployed + verified same day: 22-min in-cluster build,
authoritative seed (104,334 files, identical digest, staging copy since removed),
pod 1/1, native route 302 via the VIP and bridged `code-server.<domain>` 302 after a
traefik redeploy rendered its forward-bridge router (inventory-only changes are outside
gitops' trigger map — the redeploy has to be asked for).

Fallout caught by the shrink survey and fixed same night (`026d058b`): HA's 08-09 move
left prometheus's `home-assistant` scrape job and monitor-bridge's `HA_URL` on the dead
Docker DNS name — `ha_heartbeat` DOWN ~80 cycles, `targets` DOWN, and `check_ups`
green-but-blind (its `hass_*` source gone). Both re-pointed at the `-k8s` name; all three
verified recovered. Prometheus dropped its `apps` net (existed only for that scrape).

**Homepage widget inventory (the open question below, resolved 2026-08-10):** three
`server: my-docker` docker-status pairs remain — uptime-kuma, pihole, peanut — via
`docker-proxy:2375`, plus two widget URLs on Docker DNS names (`http://uptime-kuma:3001`,
`http://pihole:80`). All other targets already point at cluster names/VIPs. Those three
stay Docker-side until Phase D (Kuma), the Pi-hole query-log flatline, and the D6 residual
set (peanut) — so homepage ports AFTER the Phase D Kuma move, not before: doing it now
would drop the status dots AND leave widget APIs pointing through Authelia-gated bridge
names they cannot authenticate to, then need re-pointing again anyway.

### D — Monitoring consolidation (the long pole)

Per D3/D4, in strangler order: stand up cluster-side replacements → dual-run → repoint
consumers → retire Docker copy. Sub-order: Uptime Kuma + AutoKuma first (alerting spine,
and its move unblocks `monitoring_controller_host`), then Loki/promtail (terraria-stats and
probe.py's loki-query re-point), then Prometheus/Grafana dashboard triage (port only
dashboards still consulted), tempo last (lowest value, fewest consumers). otel-collector per
D7. scrutiny: collector → DaemonSet on both nodes, influxdb+web to the cluster.

### E — Docker edge retirement

Only after C and D empty the `proxy` network: Docker traefik's routers shrink to nothing,
the reverse bridge's host list (inventory-derived) shrinks in lockstep, Authelia's Docker
portal loses its last gated service and retires (its k8s twin already carries the public
cookie domain), the demoted crowdsec agent's acquisition shrinks to host logs (auth.log —
it stays as a host-log agent or hands auth.log to a node-level tailer). 80/443 published
ports disappear from daniel-server — the D2 join precondition.

### F — The join

`k3s agent` on daniel-server (Ansible-driven, same role, agent mode), node-DNS drop-in per
A1's pattern, taints/labels reviewed (media stays pinned to daniel-box's PV; nothing
schedules to daniel-server that assumes /dev/dri). Longhorn `default-replica-count` → 2 and
existing volumes raised; the settings patch slice-0 deliberately kept cheap. Gates: node
Ready; a test PVC reports 2 healthy replicas across nodes; a daniel-box drain reschedules a
stateless workload onto daniel-server and back; cold-boot both hosts (A1's gate, third run).

### G — Residual role, instruments, and the books

homelab-mcp successor + otel-collector per D7. `backup_controller_host` and
`renovate_notify_host` flip or dissolve with their anchors. kopia's runbook rewritten for
its shrunk scope (design decision 1's debt). design.md §8 marked complete; the
platform-filter guard tests collapse to their end-state (daniel-server's containers_list
carries only the residual set); a final `/homelab-review` pass over the finished shape.

## Exit criteria

1. daniel-server runs only: k3s agent, peanut/NUT, wg-easy, kopia, and node-level exporters.
   Its Docker daemon serves exactly that residual set (D6).
2. Longhorn: 2 replicas on every volume, verified healthy on both nodes; a daniel-box drain
   reschedules stateless workloads.
3. One monitoring plane: cluster Prometheus/Grafana/Loki + migrated Uptime Kuma, with the
   Pi's and both hosts' pushes landing there; the Docker monitoring stack is gone.
4. No `platform: docker` entry remains that is not in the D6 residual set; guard tests
   assert that shape.
5. The four `*_host` flags are gone or point at their post-migration anchors.
6. Cold-boot gate passes with the cluster on both nodes (third run).

## Unverified — resolve during execution, not by assuming

- **etcd/agent join flags** for an existing `--cluster-init` server — verify the k3s role's
  server-vs-agent branch renders the right systemd unit before touching daniel-server.
- **Longhorn replica raise on EXISTING volumes** — the settings patch covers new volumes;
  per-volume `numberOfReplicas` may need a patch loop. Measure on a scratch volume first.
- **Which Grafana dashboards are still consulted** — port by evidence (access, not
  inventory); the rest retire with a note.
- **promtail/journald coverage** for auth.log once the crowdsec agent question in E lands.
- **homepage widget inventory** — which widgets still point at Docker names at Phase C time.
- **Claude Code OTLP loopback** — confirm the collector DaemonSet hostPort satisfies it on
  both nodes before retiring the Docker otel-collector.
- **docker-proxy-codeserver / -lifecycle consumers** — enumerate before retiring the trio.
