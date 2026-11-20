# Airflow warehouse plan

Airflow 3 on daniel-server rolls short-retention sources up into a Postgres warehouse every
night, so Grafana can show trends over months instead of days. The work ships as nine slices
under the `Airflow homelab warehouse` milestone. Slice 0 is the platform plus one end-to-end
rollup, and each later slice adds one source. `k8s/roles/airflow/CLAUDE.md` describes the
role as built.

## Why every history store forgets

| Source | Retention | Slice |
|---|---|---|
| Prometheus | 30d or 7GB, and the size cap binds first: 22 days on 2026-11-19 | 0, 1 |
| Claude Code OTEL telemetry | ~30d | 2 |
| Home Assistant recorder | 7 days (`configuration.yaml:145`) | 3 |
| Scrutiny SMART history | forever, on `scrutiny-influxdb-data`, which is not backed up | 4 |
| GitHub PRs and CI runs | kept by GitHub, but costly to query live | 5, 6 |
| Traefik and CrowdSec counters | Prometheus, as above | 7a |
| Longhorn's backup log | `loki-homelab`, 31 days | 7b |
| Pi-hole query DB | FTL default 91 days, not checked against this config | 8 |

## Decisions

- **Hand-written Jinja manifests, not the Helm chart.** No role uses Helm, and Flux applies
  only rendered output.
- **KubernetesExecutor.** No idle workers, and no Redis or Celery.
- **Four components:** api-server, scheduler, dag-processor and Postgres. Airflow's
  deferred-task process and its metrics emitter are off.
- **DAGs are baked into the built image**, so a DAG change is reviewed, tested, and rolls the
  pods by moving the content tag.
- **Authelia two_factor in front, and Airflow's simple auth manager makes every user an
  admin.** The fences admit only the pods that call each port.
- **One Postgres, two databases**, `airflow` and `warehouse`. Grafana reads the warehouse as a
  SELECT-only role.
- **The claim is backed up weekly to B2.** The R2 daily tier is derived from home-critical
  entries only, and Airflow is not one. A lost week re-derives by backfill from sources that
  keep at least seven days.
- **The backfill window is read from the source, not declared.** Prometheus' size cap moves
  its oldest day, so the DAG reads `prometheus_tsdb_lowest_timestamp_seconds` per run.

## Conventions every DAG follows

- One partition per day, replaced in one transaction, so a backfill is always safe to rerun.
- A `check` task after each load fails the run on a missing key, an hour-long gap or a value
  out of bounds. A gap usually means the source was down.
- A day the source no longer wholly holds is skipped.
- Sources are read by ClusterIP, never through a route. Each source's NetworkPolicy gains an
  allow for `app: airflow-task`, after a caller census (ADR 0009).
- Transforms are stdlib-only and tested against recorded source responses in the role's
  `tests/`.
- Each load DAG emits an asset, and derived DAGs (slice 6) schedule on assets.

## Slices

- **Slice 0, platform plus one rollup.** The role, Postgres, Grafana's `Warehouse`
  datasource, and `prom_daily_rollup`: daily UPS energy from NUT and daily CPU package
  temperature per host. Done when `land.sh` reads healthy, the backfill fills every day
  Prometheus holds, a cleared day reloads to the same row counts, and the idle footprint is in
  the role's `CLAUDE.md`.
- **Slice 0b, the scheduler deadman.** A monitor-bridge check on the scheduler's health,
  and its alert rule, so a dead scheduler pages. Done: the `airflow_scheduler` row reads the
  scheduler's own health server, and `AirflowSchedulerDown` pages it.
- **Slice 1, long-term metrics (idea 1).** Volume usage and growth per claim, node load and
  memory, GPU busy and UPS battery, as `prom_series_rollup` into `prom.series_daily`, plus a
  "Homelab history" board. Volume usage reads kubelet's filesystem figures, because
  `actualSize` is not filesystem usage (ADR 0006). Daily B2 Class-B counts moved to slice 7b:
  Longhorn logs them, so they live in `loki-homelab`, not Prometheus.
- **Slice 2a, Claude Code usage history (idea 2).** Daily spend, tokens, sessions, active
  time and output from Prometheus' per-session counters, as `claude_usage_rollup` into
  `claude.usage_daily`. Each session counts its rise from its last value in the eight days
  before the day, merged across collector restarts; `increase()` drops every new session's
  first export, and read 14 to 41% low on each day from 2026-11-05 to 11-18.
- **Slice 2b, Claude Code decision history (idea 2).** Tool errors and permission decisions
  from the `tool_result` and `tool_decision` events in observability's Loki, as
  `claude_decisions_rollup` into `claude.decision_daily` and `claude.tool_result_daily`, with
  the four counting traps of permission-decision telemetry as tested code. Loki keeps 31
  days, so slice 7, whose sources keep 22 and 31, comes next, ahead of slices 4 to 6,
  whose sources keep far longer.
- **Slice 3, Home Assistant history (idea 3).** Daily min, time-weighted mean, max and
  unavailable time for a declared sensor list, and each energy counter's daily increase, as
  `ha_daily_rollup` into `ha.sensor_daily`. The source keeps seven days, so a longer DAG
  outage loses data. Built before slice 2 for that reason: Claude telemetry keeps about 30.
- **Slice 4, SMART history (idea 4).** Scrutiny's stored history, by ClusterIP.
- **Slice 5, repo engineering metrics (idea 5).** PRs, CI runs and findings issues,
  incremental by `updated_at`. All of one user's tokens share one GitHub quota, so each run is
  budgeted and scheduled in quiet hours.
- **Slice 6, cost per merged PR (idea 6).** Joins slices 2 and 5 on assets. The join key is
  open: check whether the telemetry carries a branch or worktree name per session.
- **Slice 7a, network edge history (idea 7).** Traefik's requests per entrypoint and route by
  status code, and CrowdSec's overflows, AppSec requests and blocks, bouncer requests and
  active decisions, as `edge_daily_rollup` into `edge.counts_daily`. The source is
  Prometheus's counters, not `loki-homelab`: Alloy drops routine Traefik access lines before
  they reach it (`k8s/roles/loki-homelab/templates/config/config.alloy.j2`). Authelia exports
  no metrics, so its counts wait for a source.
- **Slice 7b, B2 Class-B history.** Daily backups, Class B transactions and uploaded blocks
  per volume from Longhorn's log in `loki-homelab`, as `backup_spend_rollup` into
  `longhorn.backup_daily`. The log line names no backup target, so the load task reads each
  volume's `spec.backupTargetName` through the Kubernetes API, as a ServiceAccount whose one
  grant is `list` on Longhorn volumes.
- **Slice 8, DNS history (idea 8).** Both Pi-hole instances' query counts per client and
  domain.

Later, once the warehouse holds a few months: forecasting DAGs, an energy-cost model, and
weekly reconciliation checks.

## Risks

- **Losing the claim erases history the sources no longer hold.** The weekly B2 backup bounds
  the loss to the last week, which backfill restores.
- **A DAG outage longer than a source's retention loses days.** The quality check fails on a
  gap, and slice 0b pages on a dead scheduler.
- **Task logs reach Loki only when the pod outlives Alloy's tail.** A sub-second task often
  leaves no line there, so task state is read from Airflow. A remote log handler is #9202.
- **Airflow upgrades are schema migrations.** The base is pinned by digest with
  `k8s_autodeploy: false`, and the scheduler's init container migrates before anything else
  starts.
