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

daniel-server: 16 containers, 12 inventory entries, and the Docker edge routes NOTHING —
pihole retired the night of 08-13 (the desktop repointed to `dns_k8s_vip` and the query
log flatlined within the hour), and homelab-mcp rehomed to k8s the same night (pulled
forward from Phase G; it had been dark since BT3 anyway — the wildcard flip left
mcp.local resolving to a VIP where nothing routed it). The rehome hit a real buildkit
edge: a multi-file COPY from a ConfigMap-mounted build context drops all but the first
symlinked file (followPaths closure bug; `COPY .` is the workaround, in the Dockerfile
comment).

## Open gates

| Gate | Blocks | State |
|---|---|---|
| Docker loki/promtail cut | grafana role removal | **Loki DONE 2026-08-13** (operator closed the dual-write window 4 days early; history discarded with the volume, `loki-docker-retiring` datasource deleted). The grafana role does NOT retire: promtail stays a Docker-host tailer until the Phase F join (KL2), and the role remains the dashboards-tree source for the cluster grafana. daniel-server: 13 containers. |
| E7 — edge proper | — | **DONE 2026-08-13** (same day, three slices). 1: k8s Authelia claimed auth.<domain> + reinstated OIDC (Jellyfin's live SSO issuer, carried verbatim — verified by discovery doc); reverse bridge deleted. 2: traefik scrape job dropped (TARGETS_MIN 5→4), LOKI_STREAM slimmed, origin-lock/drift checks+tiles retired, AppSec verifier re-homed to daniel-box (the only WAF-enforcing signal). 3: traefik role slimmed to the demoted crowdsec agent (auth.log only — kept for SSH coverage until Phase F), authelia archived, cluster-consumed CrowdSec files re-homed to k8s/crowdsec, in-role tombstones unwound the units/crons/state, containers removed, 80/443 verified closed. daniel-server: 14 containers, 11 entries, zero routed services, no public ports. |

## Stays until Phase F/G (not this phase's debt)

monitor-bridge, autofix-bridge, docker-proxy(+lifecycle)/autoheal, nut, node-exporter,
cadvisor, otel-collector (pure forwarder), scrutiny-collector (SMART spoke),
terraria-stats, unbound (pihole's upstream, retires with it), homelab-mcp (Phase G),
everything on the Pi.

Interleaved in the same window but tracked separately: the B2 tiering rework, kopia B2
deletion, and Longhorn re-arm (`docs/longhorn-backup-tiering.md`,
`docs/longhorn-disaster-recovery.md`, `backup-consolidation-longhorn.md`).
