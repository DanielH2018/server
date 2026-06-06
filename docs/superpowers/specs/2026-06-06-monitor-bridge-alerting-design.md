# monitor-bridge — metric & backup alerting via Uptime Kuma

**Date:** 2026-06-06
**Status:** Approved (design)
**Context:** M3 from the 2026-06-06 server review. Prometheus collects metrics but fires no
alerts (its `alerting:` block has Alertmanager commented out, no rule files), and Kopia
snapshot *success* is unmonitored (only the container's liveness is). This adds threshold +
backup alerting using **Uptime Kuma as the single alerting hub**.

## Goal

One small sidecar (`monitor-bridge`) evaluates four conditions on a loop and pushes
`status=up|down` to a dedicated Uptime Kuma **push monitor** per condition — the same
dead-man's-switch pattern already used by `cloudflare-ddns`. Kuma's existing notification
provider delivers the alerts.

Non-goals: standing up Alertmanager; Grafana alerting; replacing CrowdSec→Discord (separate,
stays as-is).

## Architecture

- New container role `ansible/roles/containers/monitor-bridge/`.
- Networks: `monitoring` (reach `prometheus:9090`, `uptime-kuma:3001`) **and** `kopia`
  (reach `kopia:51515`). Joining the `kopia` net as trusted infra — like Traefik does —
  keeps Kopia off `monitoring`, so apps still can't reach the unauthenticated `kopia:51515`.
  See [[kopia-unauthenticated-intentional]].
- No web UI, no Authelia (`port: false`, `use_authelia: false`).
- Image `python:3.12-alpine`, **stdlib only** (`urllib`, `json`) — no jq, no build step.
- Process: `python /app/check.py` runs a `while True` loop (`INTERVAL`, default 300 s).

## Components

| File | Purpose |
|------|---------|
| `roles/containers/monitor-bridge/files/check.py` | **Static** checker. All config via env vars (no Jinja) so it stays plain/lintable. One `try/except` per check; supports `--once` for verification. |
| `roles/containers/monitor-bridge/templates/docker-compose.yml.j2` | Service + 4 AutoKuma push-monitor labels + env (URLs, thresholds, 4 push tokens). |
| `roles/containers/monitor-bridge/tasks/main.yml` | `copy` check.py → container dir, then `include_role: common` (`setup_dirs` + `docker_deploy`). |
| `roles/containers/monitor-bridge/meta/deps.yml` | `role_deps: [prometheus, uptime-kuma, kopia]` (deploy ordering). |
| `roles/containers/monitor-bridge/meta/main.yml` | galaxy_info, matching sibling roles. |
| `roles/containers/monitor-bridge/CLAUDE.md` | Role doc (At a glance / Notable / Editing). |

Registration: add to `inventory/host_vars/daniel-server.yml` → `containers_list`
(`name: monitor-bridge`, `port: false`, `use_authelia: false`, `networks: [monitoring, kopia]`,
`tags: [monitor-bridge]`). No `deploy.yml` edit — the dynamic loop over `containers_list`
handles inclusion.

## Hardening (matches fleet conventions)

```yaml
user: "1000:1000"
cap_drop: [ALL]
security_opt: [no-new-privileges:true]
read_only: true
tmpfs: [/tmp]
deploy:
  resources:
    limits:   { cpus: '0.10', memory: 64M }
    reservations: { cpus: '0.02', memory: 16M }
```

## The four checks

Each check returns `(ok: bool, msg: str)` and maps to one Kuma push monitor. On every loop the
bridge pushes the result. Explicit `status=down` gives fast, descriptive alerts; the Kuma push
monitor's heartbeat interval (≈3× loop) is the backstop for "the bridge itself died." Thresholds
are env-configurable and will be confirmed against live metrics during implementation.

| Monitor name | Source | `down` condition | Default threshold |
|---|---|---|---|
| Backup Freshness | Kopia API `GET http://kopia:51515/api/v1/sources` → source for `/data/home/ubuntu/server/containers`, `lastSnapshot.startTime` | snapshot age over limit, source error, or API unreachable | 30 h (24 h interval + grace) |
| Root Disk | Prometheus `node_filesystem_avail_bytes` / `node_filesystem_size_bytes` for `/` (+ LVM data mount, confirmed live) | usage exceeds limit | 90 % |
| TLS Cert Expiry | Prometheus `traefik_tls_certs_not_after` (min across certs) − `time()` | days remaining below limit | 14 days |
| Memory / OOM | Prometheus `node_memory_MemAvailable_bytes` / `node_memory_MemTotal_bytes`; cadvisor `increase(container_oom_events_total[1h])` | host mem usage over limit OR any container OOM in window | 90 % mem; OOM count > 0 |

PromQL is issued via `GET http://prometheus:9090/api/v1/query?query=…`; the bridge reads the
scalar result. Metric names (`traefik_tls_certs_not_after`, exact mountpoints) are verified live
in the first implementation step.

## Data flow

```text
node-exporter / cadvisor / traefik ─┐
                                    ├─→ Prometheus ─┐
kopia API ──────────────────────────────────────────┼─→ monitor-bridge ─→ uptime-kuma /api/push/<token> ─→ Kuma notification
                                                     ┘
```

## Error handling

- Prometheus query failure / no data → that monitor `down`, `msg="metric unavailable: …"`
  (a broken exporter is surfaced, not silently green).
- Kopia API unreachable / bad JSON → Backup Freshness `down`, `msg="kopia API unreachable"`.
- Each check wrapped in `try/except`; one failing check never blocks the others.
- The push call is best-effort: on push failure, log to stdout and continue.
- Kuma `max_retries` (default 1–2) debounces transient flaps.

## Secrets

Four push tokens in `ansible/vars/secrets.yml` (edit via `sops`), same convention as
`cloudflare_ddns_*_push_token`:
`monitor_bridge_kopia_push_token`, `monitor_bridge_disk_push_token`,
`monitor_bridge_cert_push_token`, `monitor_bridge_mem_push_token`.
Passed to the container as env vars **and** as `push_token=` in the AutoKuma labels (so the
auto-created monitor's endpoint matches what the bridge pushes to).

## Operator prerequisites (Kuma state is UI-managed / backup-excluded, not in code)

1. Generate the four push-token strings into `secrets.yml`.
2. Attach the four push monitors to the existing Kuma **notification provider** (AutoKuma
   notification label or the Kuma UI) — without it they go red but don't notify.

## Verification

1. `validate_compose_templates.py` renders the new compose with stubbed vars (pre-commit).
2. Deploy: `ansible-playbook ansible/deploy.yml --tags monitor-bridge --check`, then for real.
3. Run `check.py --once` (or watch the loop): all four monitors appear in Kuma and go green.
4. Force one breach (e.g. set the cert threshold absurdly high, or disk to 1 %): confirm the
   monitor flips red and the notification fires. Revert.

## Testing approach

`check.py` is plain Python with env-injected config and pure per-check functions, so the metric
parsing is unit-testable in isolation. The repo has no pytest harness today; the `--once` mode
plus the live verification above is the primary acceptance gate. A minimal unit test over the
PromQL-result parser can be added if we want regression coverage.
