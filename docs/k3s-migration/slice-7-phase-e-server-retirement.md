# Slice-7 Phase E — Docker server-plane retirement

Executed 2026-08-12/13, operator-approved ("let's start phase e", parallel agents per
slice). Parent: `slice-7-drain-and-join.md` (Phase E: the Docker edge and the services
still behind it stand down service-by-service). Precondition met the same day: the
livesync X-Sync-Token gate — the last bridge residual — rehomed to the k8s edge's
Secret-mounted file provider (`32625eb5`), leaving the Docker edge carrying **zero
cluster routing**.

## What landed

### E1 — grafana
Docker grafana removed; the cluster claude-otel Grafana owns the unsuffixed
`grafana.<domain>` name (ingressroute now forwards `unsuffixed_hostname`, `5f86c6dd`).
Datasource provisioning keys on NAME, so the loki→loki-homelab rename needed a top-level
`deleteDatasources` to avoid a uid-conflict CrashLoop. The role keeps loki + promtail
until the 2026-08-17 cut (Phase D.2 step 4).

### E2 — host-exporter scrape cutover, then prometheus retirement
Atomic by necessity (no LAN-reachable exporters existed, and remote-write label
collision forbade an overlap): node-exporter 9100, cadvisor 9101, promtail 9102,
crowdsec 9103, traefik 9104 published on `{{ server_ip }}` behind a DOCKER-USER
firewall admitting only daniel-box (`prometheus-exporters-lan-firewall.sh`, the
nut-lan-firewall pattern); five cluster scrape jobs with `instance: daniel-server` /
`origin: daniel-server` static labels for dashboard continuity; the Docker prometheus
jobs dropped in the same change.

The prometheus container itself retired 2026-08-13 (`b6cad82e`, `bdb1a3d8`): by then its
only reader was monitor-bridge's `check_remote_write`, which watches the remote-write
pipe that exists only because the Docker prometheus does — check, 15 tests, kuma tile,
push token, compose service and `prometheus_data` volume all removed. The role is now
exporters-only (see its CLAUDE.md).

**This supersedes PG4** (`slice-7-phase-d-dashboards.md`: "Docker prometheus AND grafana
keep running until Phase F"). The operator's Phase E go-ahead traded away the
pre-remote-write TSDB history (series before ~2026-08-07 that the cluster never
received) — `prometheus_data` was deleted with the container. Everything since 08-07
lives in the cluster prometheus.

### E3 — homepage
Rehomed to k8s (`8795e161`, `f3bffdb7`). Config stays sourced from the Docker role's
templates via `lookup('template')` except `services.yaml.j2`, which is forked (Docker
widget `server:`/`container:` keys, peanut URL) — the hand-sync obligation is enforced
by `ansible/tests/test_homepage_services_fork_sync.py` (`c2df63e8`). Traps encountered,
all fixed in-role: kubelet probes send the pod IP as Host (probe `httpHeaders` pin),
`kubernetes.yaml` must exist (EROFS crash loop), icons embed via
`lookup('pipe', 'base64 -w0 …')` (lookup('file') mangles binary). Widgets that rode the
dead bridge routes now use ClientIP-gated monitoring routes with the pod CIDR
(`10.42.0.0/16`) admitted.

### E4 — peanut
Web UI rehomed to k8s; **nut itself stays host-side** (USB-attached UPS; upsd 3493 was
already LAN-published and firewalled to daniel-box). The app writes `/config`, so an
alpine initContainer seeds a writable emptyDir from the read-only Secret mount.

## State after this phase

daniel-server: 19 containers, 14 inventory entries. The Docker edge routes exactly two
services: pihole and homelab-mcp.

## Open gates

| Gate | Blocks | State |
|---|---|---|
| pihole query flatline | pihole retirement | Desktop (10.0.0.140, ~4.3k queries/day) repointed to `dns_k8s_vip` ~00:10 UTC 2026-08-13; re-measure via `migration-oneshots/pihole-query-volume.yml`. daniel-box's ~456/day is cluster monitor traffic that dies with the tile. DHCP is router-owned (pihole DHCP verified off). |
| Docker loki/promtail cut | grafana role removal | Calendar: 2026-08-17 (dual-write window). Decide the `loki-docker-retiring` datasource / history sign-off at the cut. |
| E7 — edge proper | traefik, authelia, crowdsec, 80/443 unpublish, DOCKER-USER crons | Blocked by pihole (above) AND homelab-mcp, which stays until Phase G — E7 cannot complete before homelab-mcp rehomes or its route is otherwise served. |

## Stays until Phase F/G (not this phase's debt)

monitor-bridge, autofix-bridge, docker-proxy(+lifecycle)/autoheal, nut, node-exporter,
cadvisor, otel-collector (pure forwarder), scrutiny-collector (SMART spoke),
terraria-stats, unbound (pihole's upstream, retires with it), homelab-mcp (Phase G),
everything on the Pi.

Interleaved in the same window but tracked separately: the B2 tiering rework, kopia B2
deletion, and Longhorn re-arm (`docs/longhorn-backup-tiering.md`,
`docs/longhorn-disaster-recovery.md`, `backup-consolidation-longhorn.md`).
