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

> **Residual set shrank in execution.** wg-easy left at slice-6 B5 (the operator moved all
> router forwards to daniel-box, so the workload followed its port), and kopia RETIRED
> outright at the 2026-08-10 backup consolidation instead of shrinking — the Longhorn plane
> absorbed everything it uniquely protected (`backup-consolidation-longhorn.md`), its B2
> repo was deleted 08-13, and the residual object versions were purged 08-14. The residual
> set is therefore: k3s agent, peanut/NUT, and node-level exporters.

> **D6 REVERSED (operator, 2026-08-14): the residual role dissolves entirely.** "Add it to
> K3s so that docker can be entirely removed" — nut becomes the k8s/nut pod, pinned right
> back to daniel-server for the USB (a physical pin, not a drain artifact). Two things made
> the original trade obsolete in execution: (1) the shutdown chain was ALREADY host-native
> (the secondary upsmon is host `nut-client`, not Docker — the pod re-creates the same
> `127.0.0.1:3493` loopback publish as a hostPort, so the poweroff path is byte-identical
> and gains no new failure coupling), and (2) the registry re-plumb the drain deferred got
> a cheap shape: same `localhost:5000` mirror key on every node, endpoint = loopback
> hostPort on daniel-box / pinned ClusterIP over flannel on agents (plain HTTP inside the
> vxlan, accepted), proven per-node by a second, daniel-server-pinned pull selftest. The
> pod is `privileged: true` — k8s has no compose `devices:` equivalent, so the Docker
> cap-list + udev posture cannot be expressed; a USB device plugin is the noted follow-up.
> Host half (udev rule + secondary upsmon) moved to the `nut_host` role
> (`initial_setup.yml`, gated on `ups_host`). The nut-lan-firewall + LAN publish retired
> (consumers — peanut web, HA — use the `nut` ClusterIP Service). End state: daniel-server
> runs the k3s agent and NOTHING under Docker; the endgame (§G) now includes the two host
> flips, remnant-bridge dissolution, and full Docker uninstall (`has_docker: false`).

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

**FLIPPED 2026-08-10 ~04:00 UTC (operator).** (First recorded as ~17:55 UTC — that was
when it was *noticed*; the Docker edge's public-traffic flatline in Prometheus puts the
actual flip at ~04:00, matching the operator's "about a day" soak estimate.) Verified
from the cluster edge's own metrics: the reverse-bridge router carries live traffic (it
only matches public arrivals for Docker-hosted names), public apps serve through the k8s
Traefik, and no monitor paged across the flip. LTE-verified by the operator ~18:00 UTC
the same day. Still open
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

**Terraria disposition DECIDED (2026-08-13, operator):** the game server is staying —
terraria + terraria-stats MIGRATE to the cluster at the Phase F drain (not retired). Plan
them as a normal port: the server's world state moves to a Longhorn PVC, stats re-points
at the cluster Loki (already done at D step 3 if executed in order).

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

**JOIN EXECUTED 2026-08-13** (operator moved F up; storage participation deliberately
deferred until the B2 recovery proves the backup plane): daniel-server is Ready +
cordoned, agent mode in `roles/setup/k3s/tasks/agent.yml` + the opt-in three-play join in
k3s-bringup.yml (`-e join_agent=daniel-server`; play-level ssh override — the hosts.ini
`local` connection is for hosts running their own plays, and play connection vars poison
delegation, hence three plays handing facts via hostvars). Execution findings, each
codified in-role:

- **ETP=Local VIP blackout** — the join's one real incident. kube-proxy on the new node
  programs all six MetalLB VIPs, and with no local traefik endpoint the filter-table
  KUBE-EXTERNAL-SERVICES chain silently DROPs forwarded (container) traffic to them:
  every Docker container dialling the cluster crash-looped or went blind within minutes
  (host-level probes stay green — false all-clear). Fix: vip-kube-bypass.sh + 5-min
  position-reassert timer (nat PREROUTING RETURN + filter FORWARD ACCEPT for
  10.0.0.240/28) — container VIP traffic egresses to the LAN masqueraded as the node IP,
  the pre-join path. Retires at the drain.
- **L2 announcements pinned to daniel-box** (metallb-pool.yaml.j2 nodeSelectors) — the
  new speaker had won the .240 election within minutes of joining.
- **promtail + scrutiny-collector DaemonSets pinned to daniel-box** (nodeSelector) —
  unpinned they land on the new node and double-ship authlog/syslog / double-collect
  disks next to the still-running Docker copies. Pins come off at the drain.
- **`create-default-disk=false` node-label is INERT** (needs the cluster-level
  `create-default-disk-labeled-nodes` setting) — a default disk appeared with
  allowScheduling:true; agent_verify.yml now patches the Longhorn node CR to
  `allowScheduling:false` and asserts zero replicas on the node.
- **No A1 DNS drop-in on this node** (static resolv.conf already falls through to
  1.1.1.1; an upstream-only pin would break container `.local` resolution) and **no
  registries.yaml** (loopback registry unreachable cross-node — image-built workloads
  stay pinned to daniel-box until the drain re-plumbs it).

**Still open in §F:** the replica raise + allowScheduling flip (after a green nightly),
the test-PVC/drain-reschedule/cold-boot gates, then uncordon.

**DRAIN EXECUTED SO FAR (2026-08-13, same day as the join):**
- **scrutiny collector spoke** (13 → 12): the DaemonSet's daniel-box pin came off (both
  hosts share the /dev/nvme0 controller path — verified before the unpin); Docker spoke
  + role archived. The new node's first collection is the midnight cron;
  scrutiny_freshness alarms a miss.
- **otel-collector forwarder DISSOLVED** (12 → 11): the cluster collector is a DaemonSet
  — every node has its own loopback OTLP hostPort, the seam the D7 forwarder faked for
  daniel-server. The ClientIP-gated ingest IngressRoute retired with it (nothing ingests
  over the LAN). The prometheus otel jobs moved to per-POD discovery in the same commit:
  each collector exports only what ITS node received, so a Service-target scrape would
  round-robin partial views and corrupt the claude_code_* counters. Verified: both
  loopbacks answer, 4 per-pod targets up, exports resumed against the new pods.
  Cluster-side deletes (old Deployment, ingest route) needed explicit kubectl deletes —
  apply-only deploys never remove, and a Deployment→DaemonSet rename-in-kind leaves both
  holding the same hostPort.

- **terraria-stats** (11 → 10): all-time SQLite seeded onto a daily-tier Longhorn PVC
  (totals outreach Loki's 28d backfill), script from a --from-file ConfigMap on stock
  python:alpine (no registry coupling). The move re-lit the player-stats board — nothing
  had scraped the exporter since the Docker prometheus retired. Verified: target up,
  both players' totals intact through the move.
- **crowdsec agent re-homed** (10 → 9; traefik role archived, its entry retired 8 → 7):
  a crowdsec-node-agent DaemonSet tails each node's auth.log against the in-cluster
  LAPI — daniel-box's host SSH signal is NEW coverage (the engine pod is AppSec-only).
  Per-node machines k8s-node-agent-<node>; /var/log mounted as a DIRECTORY (a
  single-file hostPath pins the inode across logrotate — the Docker file-bind's silent
  post-rotation gap). 9103 + its firewall port retired, TARGETS_MIN 4 → 3, the
  DaemonSet scraped per-pod. Verified: both machines validated, both targets up, both
  agents reading auth.log lines; old daniel-server-agent machine deleted.
- **Registry pins (drain-prep):** every pod template with a locally-built image (n8n ×2,
  ical-proxy, code-server, homelab-mcp, registry + self-test jobs, image-builder's build
  job) carries an explicit daniel-box nodeSelector so the uncordon can't strand one in
  ImagePullBackOff against the loopback-only registry.

Drain order for what remains: monitor-bridge + autofix-bridge (the long pole), THEN
node-exporter/cadvisor/promtail (they watch the Docker estate, so they leave last), with
docker-proxy(+lifecycle)/autoheal retiring alongside their final tenants. nut stays.
The claude-otel "changed-run flake" is CLOSED — never a flake: after the DS cutover,
assert_stable still read otel-collector as a Deployment, and the assert was change-gated,
so exactly (and only) changed-manifest runs failed. Root-caused + fixed by the
refinements pass (kind-aware rollout lists, PR #122). Nothing left to capture.
Drain bookkeeping since that pass: monitor-bridge also carries K8S_MIN_DAEMONSETS=9 —
retiring or adding a DaemonSet during the drain must bump that floor with the same
narrative discipline as the other MIN guards.

### G — Residual role, instruments, and the books

homelab-mcp successor + otel-collector per D7. `backup_controller_host` and
`renovate_notify_host` flip or dissolve with their anchors. kopia's runbook rewritten for
its shrunk scope (design decision 1's debt). design.md §8 marked complete; the
platform-filter guard tests collapse to their end-state (daniel-server's containers_list
carries only the residual set); a final `/homelab-review` pass over the finished shape.

**homelab-mcp successor EXECUTED 2026-08-13** (pulled ahead of the drain — pure code
against the existing cluster): the dark Docker-socket tools have cluster-API successors
— `list_pods` / `workload_status` / `list_nodes` / `pod_logs`, reading the Kubernetes
API with the pod's own ServiceAccount (rbac.yaml: get/list on pods, pods/log, nodes,
deployments, daemonsets — narrower than the shell's homelab-readonly SA, no watch). The
Docker originals keep their clear dark error and retire with the drain.
`claude_code_events` (dark since D7) is re-lit against the claude-otel Loki with the
KL1 boundary enforced in code: rows project through `k8s_reads.CLAUDE_EVENT_FIELDS`
(a whitelist — event_name/tool_name/decision/model/…), the log body is never returned
in any form, and the projection is unit-tested against a content-bearing fixture.
Verified end-to-end through the bearer gate 2026-08-13 (all four new tools + the
whitelist observed from the client side). Logic lives in `files/k8s_reads.py`
(offline-tested, same contract as safe_reads.py); trap for the next tool: the
image-builder context is an EXPLICIT file map in tasks/main.yml — a new module must be
added there or the image builds without it (found via CrashLoopBackOff, plus the
in-place-rebuilt tag needing a rollout restart to re-pull).

The REST of §G stays anchored to the drain: host flips, design.md §8, guard-test collapse,
the final review pass. The kopia runbook rewrite EXECUTED 2026-08-14 — with a changed
premise: kopia didn't shrink (the design-decision-1 debt this line anticipated), it retired
entirely at the consolidation. `kopia-disaster-recovery.md` is frozen as a historical doc,
its still-live content (the off-site recovery kit, the external dead-man's-switch record)
re-homed into `longhorn-disaster-recovery.md`, and `secret-rotation.md`'s pinned section
now records `kopia_password` as removed (8edb11cd) rather than pinned-pending-deletion.

## Exit criteria

1. daniel-server runs only: k3s agent, peanut/NUT, and node-level exporters (the D6
   residual set as amended — wg-easy left at slice-6 B5, kopia retired at the
   consolidation). Its Docker daemon serves exactly that residual set.
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

### DRAIN EXECUTED 2026-08-14 — the bridge split (the long pole lands)

Overnight, after the backup plane was proven (nightly green at 03:38 on 41 two-replica
volumes; operator waived the full-nightly gate earlier on a clean manual backup probe):

- **monitor-bridge split, not moved** (10b62b5a, 4555a523, #138, #139): a cluster twin
  runs the 21 metric/API checks and the Docker remnant keeps the 5 host-state-file checks
  — one check.py, split by CHECKS_ONLY/CHECKS_SKIP with a startup guard refusing a gated
  check without its gate, and a test asserting the two envs PARTITION the token set. The
  25 twin-owned monitor declarations were already in the kuma-static-monitors Secret; the
  five remnant labels follow their pusher. Twin gotchas found live: /run/secrets shadows
  the SA token mount (repo guard caught it pre-deploy) and Secret-volume files need
  fsGroup + 0440 or non-root file-mounted credentials silently self-disable (#138).
- **autofix-bridge's sidecar moved** (4555a523, #139): the cluster twin runs the *arr
  remediation loop (DRY_RUN=false unchanged); the Docker container was removed inside the
  twin's 15-min GRACE_CYCLES no-act window, so no double-remediation was possible. The
  role keeps only the disk-autoprune host plane. daniel-server: 8 containers.
- **Master pushes are PR-gated now** (guard appeared mid-session ~05:00); the cutover's
  second half landed as #138/#139. Note: `git push -u origin <branch>` trips the guard's
  master-detection — push without -u.
- **B2 cap re-exceeded by the recovery itself** (retention catch-up + purge + nightly):
  target flaps unavailable on failed `backup ls` polls at poll cadence — NOT the retry
  storm (that was the storage-cap shape, unresolvable by retry). Deliberately left armed:
  the transaction cap resets 00:00 UTC before the 03:30 nightly. B2 Reachable +
  k3s Longhorn Backup stay truthfully red until then.

### GATES EXECUTED 2026-08-14 — drain-reschedule + cold boots (§F closed)

Operator-approved same-day. All three §F disruptive/storage gates now PASSED; findings
each folded back into source:

- **Drain-reschedule (daniel-box)**: ~26 unpinned workloads rescheduled to daniel-server
  and ran; Longhorn volumes failed over and back; routed-edge blackout during the window
  as predicted (L2 pin + ETP-Local). Three real finds: (1) the agent join lacked the
  kubelet `allowed-unsafe-sysctls` flag — wg-easy churned 707 SysctlForbidden pods
  (#141); (2) the 917a7402 nodeSelector pins had NEVER been applied live for
  n8n/ical-proxy/code-server — cluster manifests do NOT deploy via gitops, only via
  explicit deploys (an earlier report claimed otherwise; corrected). Applying them
  through the n8n role deadlocked on its seed pod vs the displaced RWO holder — the pins
  were patched live instead, identical to the committed templates; (3) **multipathd
  claims fresh /dev/longhorn devices and mounts fail** — the node CRs' standing
  Multipathd:False warning finally bit on the mass reattach; fixed with the Longhorn KB
  blacklist on both nodes, now codified in both host-prep flows.
- **Cold boot daniel-server**: node rejoined Ready, all 8 Docker containers auto-started
  healthy with networks intact, vip-kube-bypass timer survived, 27 reboot-killed
  replicas rebuilt from the intact daniel-box copies (41/41 healthy in ~10 min).
- **Cold boot daniel-box**: operator-run; both nodes Ready and 41/41 volumes healthy
  within a minute of boot, full pod estate settled clean. The session's cron survived
  via resume (CronList-before-CronCreate held).
- **Open item**: the crowdsec-appsec-verify root cron's Kuma pushes do not land from
  cron context (manual runs work, path verified end-to-end); suspect root-under-cron
  DNS. Re-check the tile post-reboot; escalate to the final review if still red.
