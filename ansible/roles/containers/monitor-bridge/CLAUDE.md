# monitor-bridge — metric & backup alerting → Uptime Kuma

A tiny sidecar that turns Prometheus metrics and Kopia backup state into Uptime Kuma
**push** monitors, so threshold/backup problems actually page. See repo-root `CLAUDE.md`.

## At a glance
- **Image:** `python:3.12-alpine` (stdlib only — no build, no extra deps)
- **Host:** daniel-server · **No web UI**, no Authelia
- **Networks:** `monitoring` (reach `prometheus:9090`, `uptime-kuma:3001`) + `kopia`
  (reach `kopia:51515`). Joins the `kopia` net as trusted infra — like Traefik — so Kopia
  stays off `monitoring` and apps still can't reach the unauthenticated `kopia:51515`.
- **Depends on:** prometheus, uptime-kuma, kopia (`meta/deps.yml`)
- **Config in:** `ansible/inventory/host_vars/daniel-server.yml` → `containers_list`

## Notable
- `files/check.py` is a **static** Python loop (config via env vars, no Jinja). Every
  `INTERVAL` (300 s) it runs nine checks and pushes `status=up|down&msg=…` to one Kuma push
  monitor each:
  - **Backup Freshness** (Kopia `/api/v1/sources` last-snapshot age + errorCount)
  - **Root Disk** (`node_filesystem_*`)
  - **TLS Cert Expiry** (`traefik_tls_certs_not_after`)
  - **Memory** (host `node_memory_*` pressure only)
  - **Container Restarts** (`changes(container_start_time_seconds[15m]) > RESTART_MAX`)
  - **Container OOM** (`increase(container_oom_events_total[1h]) by (name)` — names the
    offender; supersedes the old host-aggregate OOM that lived in the Memory check)
  - **CPU Throttling** (`rate(container_cpu_cfs_throttled_periods_total[15m]) /
    rate(container_cpu_cfs_periods_total[15m]) > CPU_THROTTLE_PCT` by name — catches a
    container pinned at its `deploy.resources` cpu cap, which throttles silently without
    OOM/restart/5xx. Unlimited containers give 0/0→NaN and are ignored)
  - **Scrape Targets** (`up == 0` — names the down job)
  - **Traefik 5xx** (5xx ratio over 5m, gated by a `TRAEFIK_MIN_RPS` volume floor)
- The restart/OOM/cpu/target/5xx checks use `prom_vector()` (keeps series labels) so the alert
  names *which* container / target / route is failing; the others use `prom_scalar()`.
- Explicit `down` = fast, descriptive alert; the push monitor's heartbeat interval (600 s,
  2× the loop) is the backstop for "the bridge itself died". Same dead-man's-switch idea as
  `cloudflare-ddns` — see [[its CLAUDE.md]] and the `kuma(..., monitor_type='push')` macro.
- Push tokens (`monitor_bridge_{kopia,disk,cert,mem,restarts,oom,cpu,targets,traefik}_push_token`)
  live in `secrets.yml`; we set them and Kuma honors client-supplied tokens. They're passed
  both as env (what the script pushes to) and as `push_token=` in the AutoKuma label.
- Thresholds are env-tunable in the compose template (`BACKUP_MAX_AGE_H`, `DISK_MAX_PCT`,
  `CERT_MIN_DAYS`, `MEM_MAX_PCT`, `RESTART_WINDOW`/`RESTART_MAX`, `OOM_WINDOW`,
  `CPU_WINDOW`/`CPU_THROTTLE_PCT`, `TRAEFIK_5XX_PCT`/`TRAEFIK_MIN_RPS`). A failed
  query/unreachable source makes that monitor `down` with an explanatory msg — a broken
  exporter is surfaced, not silently green.

## Operator prerequisites
1. Add the nine push tokens to `secrets.yml` (`sops ansible/vars/secrets.yml`). **They must
   be exactly 32 alphanumeric chars** (Kuma rejects others, e.g. `openssl rand -hex 16`);
   AutoKuma silently refuses to create the monitor otherwise (`Invalid push_token`).
2. Notifications attach **automatically** — the `kuma()` macro tags every monitor with
   `notification_name_list=["{{ kuma_notification_id }}"]`, linking it to the AutoKuma-managed
   Discord notification defined on the `uptime-kuma` container. No per-monitor UI clicking.

## Editing & testing
- Compose: `templates/docker-compose.yml.j2` · Logic: `files/check.py`
- Unit tests (parsing + every check's decision logic):
  `uv run pytest ansible/roles/containers/monitor-bridge/files`.
  Also run automatically by the `pytest` prek hook (`prek run pytest --all-files`).
- Smoke test one pass: `docker exec monitor-bridge python /app/check.py --once`
- Deploy: `ansible-playbook ansible/deploy.yml --tags "monitor-bridge"`
