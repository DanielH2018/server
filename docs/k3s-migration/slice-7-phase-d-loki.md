# Slice-7 Phase D.2 — Loki to the cluster (promtail stays, for now)

Planned 2026-08-10 from the coupling survey. Parent: `slice-7-drain-and-join.md` (D3).
Un-darkens terraria-stats, re-points `probe.py loki-query`/`alerts`, retires the Docker
Loki container. Decisions KL1–KL6; execution after the backup-consolidation window closes.

## What the survey established

- Docker Loki (`:latest`, unpinned, named volume, 31 d retention, 50 MB/s limits) is fed by
  Docker promtail (authlog / syslog / traefik / docker jobs) and the Docker otel-collector
  (Claude Code's daniel-server telemetry — so the Docker Loki ALREADY holds prompt content;
  the cluster Loki's loopback-only posture protects a copy of the same data class).
- Readers pin two label contracts: `container=<name>` (probe.py alerts, alert-history
  board, terraria-stats) and `job=authlog|syslog|traefik` (monitor-bridge's ingestion arm).
- The cluster (claude-otel) Loki is pinned 3.3.0, 10 Gi `longhorn-nobackup`, loopback
  hostPort only — **no LAN path exists**; the prometheus query/remote-write IngressRoute is
  the in-repo precedent for adding one.
- Nothing ships cluster pod logs anywhere — which is why terraria-stats went dark when
  terraria moved, and why janitorr's log watchdog retired at its move.
- CrowdSec reads the same three host files promtail tails, but from disk — no Loki
  coupling; that seam stays for Phase E.
- `probe.py resolve_ip()` runs `docker inspect loki` — every probe Loki path dies with the
  container unless re-pointed.
- Prometheus still scrapes `loki:3100`/`promtail:9080` on Docker DNS — the exact stale-job
  shape that bit HA and Kuma; must move in the same commit as the cutover.

## Decisions

### KL1 — A SECOND cluster Loki (`loki-homelab`), not growth of claude-otel's

Slice-3 D1 ("grow claude-otel, don't build a second") yields here, deliberately: absorbing
the homelab streams would mean giving the claude-otel Loki the LAN route it deliberately
lacks, reopening "loopback is the whole access control" for a store of verbatim prompts —
and with `auth_enabled: false` on both sides, tenancy can't separate readers cheaply.
A second instance reuses the same manifest shape (pinned 3.3.0, Recreate, compactor
retention) with its own PVC and the Docker side's rate limits carried over. The D1
exception is this paragraph.

### KL2 — promtail STAYS a Docker-host tailer until the Phase F join

It tails /var/log and Docker containers on a host that is not yet a cluster node — a
DaemonSet cannot see them. The move is therefore Loki-only: promtail keeps its exact
scrape configs (label contracts preserved verbatim, by construction) and gains a new push
URL. promtail converts to a DaemonSet at Phase F, when daniel-server becomes a node and
the whole question collapses.

### KL3 — a minimal cluster log-shipper DaemonSet on daniel-box, for pod logs

The piece that actually un-darkens terraria-stats: cluster pod logs ship to loki-homelab
with `container=<k8s container name>` and `job=k8s` labels — `container="terraria"`
resolves again, and future cluster services get log-based checks back (the janitorr
regression class). Grafana Alloy or promtail-as-DaemonSet, whichever config is smaller;
labels minted to match the existing contract.

### KL4 — access path: ClientIP-gated IngressRoute, the prometheus precedent

`loki-homelab-k8s.local.<domain>` with push + query paths, ClientIP daniel-server plus the
pod CIDR (the OR'd single-arg matchers — the Traefik v3 lesson), rate-limit middleware, no
Authelia (LogQL clients can't 302-dance; ClientIP is the gate, as for every monitoring
route). Consumers re-point: promtail client URL, monitor-bridge `LOKI_URL`, terraria-stats
`LOKI_URL`, homelab-mcp's default, probe.py (which also drops `docker inspect` resolution
for Loki), Docker Grafana's datasource, the Kuma `Loki` static entity (transcribed with
the new URL — one of the three deliberate leftovers from the Kuma cutover).

### KL5 — history continuity: 7-day dual-write, then cut

promtail (and the Docker otel-collector's Loki exporter) push to BOTH Lokis for 7 days —
`clients:` is a list; this is native. That covers `probe.py alerts --days 7` and the
alert-history board's default window. Older history stays in the Docker Loki volume until
the cut; a >7 d lookback during the overlap can still query the old instance. After the
cut, the Docker loki container retires (16 → 15) and the `loki` named volume goes with it.

### KL6 — scrapes and guards move in the cutover commit

Docker prometheus drops its `loki` job (cluster prometheus scrapes loki-homelab natively);
`promtail:9080` stays (promtail is still Docker). The config-validate corpus follows the
configs; the Watchtower-autoupdate allowlist loses loki; the image is Renovate-pinned like
the claude-otel one.

## Execution order

1. Build `roles/k8s/loki-homelab` (+ the KL3 shipper) — deploy, verify `/ready` via the
   route from daniel-server and pod logs arriving (`{job="k8s"}` non-empty).
2. Dual-write: promtail + otel-collector gain the second client URL; verify both Lokis
   ingest (the same LogQL answers on both).
3. Re-point readers (KL4 list) — verify terraria-stats un-darkens (its exporter serves
   non-zero series again), `probe.py alerts` reconstructs against the new instance.
4. After 7 days AND after D7 moves the otel-collector: drop the dual-write, retire the
   Docker loki container + volume, drop the Docker prometheus loki job, transcribe the
   Kuma `Loki` entity's URL. The D7 gate is a KL5 execution finding: the Docker
   otel-collector ships Claude content into the Docker Loki, and that stream must NOT
   dual-write to the LAN-readable homelab store (the KL1 boundary) — so the Docker Loki
   lives until the collector's own migration re-homes it to the loopback-only claude-otel
   stack.

## Unverified — resolve during execution

- Docker Loki volume size (sizes the new PVC; read it before step 1).
- Whether Alloy or promtail-DaemonSet has the smaller config for KL3.
- otel-collector's Loki exporter accepting two endpoints (may need a second exporter block
  rather than a list).
- terraria-stats' cursor behavior against an empty-then-filling stream (it may need a
  cursor reset on re-point).
