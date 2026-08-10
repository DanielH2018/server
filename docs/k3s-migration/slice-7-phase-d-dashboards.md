# Slice-7 Phase D — Prometheus/Grafana dashboard triage

Planned 2026-08-10 from the coupling survey. Parent: `slice-7-drain-and-join.md` (Phase D:
"port only dashboards still consulted"). **Operator decision overriding that phrasing:
keep ALL dashboards, as much as possible** — so the triage is not a pruning exercise; it
is making every board's *data* survive the eventual Docker-plane retirement. Decisions
PG1–PG5.

## What the survey established

- All 15 shared boards already render in the cluster Grafana (the claude-otel role stages
  the same tree), and every Docker-prometheus series already reaches the cluster instance
  via remote-write stamped `origin: daniel-server`. "Keep all dashboards" is therefore
  already true today — the risk is entirely about which *scrape jobs* die with the Docker
  prometheus and when.
- Ten Docker scrape jobs split three ways: two whose targets are ALREADY cluster
  workloads scraped from the wrong side (`uptime-kuma`, `home-assistant`); five that read
  things only daniel-server has (`node`, `cadvisor`, `traefik`, `crowdsec_daniel-server`,
  `terraria-stats`); three that retire with their subjects (`prometheus` self, `loki`,
  `promtail`).
- Two documented premises are false and get fixed here: the cluster prometheus does NOT
  scrape loki-homelab (slice-7-phase-d-loki.md:75 says it does), and the k8s Traefik
  exposes prometheus metrics that nothing scrapes.
- `monitor_status` has exactly one producer path (the Docker `uptime-kuma` job) and its
  consumers are the uptime-kuma board and `scripts/postflight.py`; monitor-bridge's
  hass/UPS checks query `hass_*` WITHOUT an origin pin, so a native port keeps them fed.

## Decisions

### PG1 — port the two wrong-side jobs to the cluster prometheus, natively
`uptime-kuma` and `home-assistant` jobs move into the claude-otel prometheus config with
the SAME job names (query continuity), credentials via mounted-Secret `password_file`/
`credentials_file` (not inline in the ConfigMap). The Docker jobs drop in the same
commit — an overlap would double `hass_*`/`monitor_status` series under sum().

### PG2 — add the two missing cluster jobs
`loki-homelab` (svc :3100/metrics — keeps Logs/loki-internals.json alive past the 08-17
Docker-loki cut, and repairs the loki-doc premise) and `traefik-k8s` (the k8s edge's
already-exposed metrics — Networking/traefik-custom.json starts showing the edge that
actually serves production).

### PG3 — the five daniel-server-only jobs stay, by design
`node`, `cadvisor`, `traefik` (Docker edge, retires Phase E), `crowdsec_daniel-server`
(demoted agent), `terraria-stats` — the slice-3 split rule ("does it read something only
daniel-server has") still holds. They keep flowing to the cluster via remote-write, so
every board stays populated in BOTH Grafanas. Their future is the Phase F join
(node-exporter/cadvisor as DaemonSets), not this phase.

### PG4 — Docker prometheus AND grafana keep running until Phase F
The keep-all-dashboards decision makes early retirement pure loss: the Docker prometheus
TSDB holds pre-remote-write history (before ~2026-08-07) that the cluster never received,
and the Docker grafana costs nothing. Cleanups only: delete the commented `netdata-scrape`
block; `postflight.py`'s Kuma gates re-point to the cluster prometheus query route (its
`monitor_status` source moves there with PG1).

### PG5 — dashboards: no deletions, one dead-data note
Every board stays in the shared tree and both Grafanas. `Apps/backups.json`
(`kopia_b2_billable_bytes`) stopped receiving data when kopia retired — it stays as a
historical view until its metrics age out of retention; a Longhorn-backup successor board
is Phase E backlog, not this phase.

## Execution order

1. PG1+PG2: cluster prometheus config gains four jobs (+ scrape-credential Secrets);
   Docker prometheus drops `uptime-kuma`, `home-assistant`, and the netdata comment.
   Deploy both sides same window; verify `monitor_status`, `hass_*`, `loki_*`,
   `traefik_*` (k8s labels) all present in the cluster prometheus and the four boards
   render against it.
2. PG4: re-point postflight.py; verify its Kuma gates pass against the cluster instance.
3. Docs: fix the slice-7-phase-d-loki.md premise; record execution here.

## Verification bar

- `monitor_status` and `hass_sensor_*` present in the cluster prometheus WITHOUT the
  `origin` label (native), absent from fresh Docker-prometheus scrapes (jobs dropped).
- monitor-bridge UPS/HA checks and Scrape Targets stay green across the cut
  (`TARGETS_MIN=5` still cleared by the 8 remaining Docker jobs).
- Logs/loki-internals.json and Networking/traefik-custom.json show fresh cluster-side
  series in the cluster Grafana.
