# airflow — nightly rollups of short-retention sources into a Postgres warehouse

Airflow 3 with the KubernetesExecutor, plus one Postgres holding the metadata database and the
`warehouse` database Grafana reads. See repo-root `CLAUDE.md` for shared conventions, and
`docs/airflow-warehouse-plan.md` for the slices still to come.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates or containers_list entry, or the Longhorn tier lists in k8s/roles/longhorn-reconcile/defaults/main.yml. -->
- **Applied by:** Flux, through `promote`; `land.sh --tags=airflow` gates the rollout
- **Images:** `ghcr.io/danielh2018/airflow` (`airflow_k8s_image`),
  `<k8s_ghcr_pull_host>/airflow` (`airflow_k8s_task_image`), `postgres`
  (`airflow_k8s_postgres_image`)
- **Route:** `airflow.<domain>` · `airflow.local.<domain>`, Authelia two_factor
- **Claim:** `airflow-postgres-data` (daily -> R2)
<!-- /generated_from -->

- **Four Deployments.** `airflow` is the api-server: the UI behind Authelia two_factor, and
  the execution API every task pod reports to. `airflow-scheduler` launches one pod per task.
  `airflow-dag-processor` parses the DAGs. `airflow-postgres` holds both databases on
  `airflow-postgres-data`. Each prefers daniel-server
  (`k8s/roles/airflow/defaults/main.yml:airflow_k8s_node`).
- **The DAGs are baked into the image.** `k8s/roles/airflow/templates/Dockerfile.j2` copies
  `files/dags/` to `/opt/dags` and runs `k8s/roles/airflow/files/check_dags.py`, so a DAG that
  fails to import fails the build. A DAG edit moves the content tag, and the next promotion
  rolls every Airflow pod. `k8s/roles/airflow/files/dags/homelab_warehouse/` holds the DAGs'
  shared code. `.airflowignore` keeps the dag-processor from parsing it, and the image's
  `PYTHONPATH` makes it importable, because Airflow 3 does not put the DAG folder on
  `sys.path`.
- **The scheduler's init container runs `airflow db migrate`.** The api-server and the
  dag-processor wait in `airflow db check-migrations` before they start. The scheduler is
  `Recreate`, so two migrators never run at once.
- **Task pods hold no metadata credential.** The `airflow` Postgres role is the image's
  superuser, so only the api-server, scheduler and dag-processor hold its password.
  `k8s/roles/airflow/templates/pod-template.yaml.j2` gives task pods the execution-API URL
  and the `warehouse` connection as `warehouse_writer`, which owns that database alone and
  cannot connect to `airflow`. Their image is the
  GHCR ref written out, because the renderer's GHCR rewrite does not reach ConfigMap text
  (`k8s/roles/airflow/defaults/main.yml:DECIDED: the task pods' image is the GHCR ref`).
  `tests/services/test_airflow_task_image.py::test_the_task_pod_template_names_the_schedulers_image_at_ghcr`
  holds it to the scheduler's.
- **Three additive fences.** `k8s/roles/airflow/templates/networkpolicy.yaml.j2` admits
  `airflow-task` and `airflow-dag-processor` to 8080, from the entry's `netpol_from`. The same
  file admits `monitor-bridge` to the scheduler's health server on 8974, from the entry's
  `airflow-scheduler` item under `netpol_fences`; that port also sets the containerPort, the
  `airflow-scheduler` Service and the server's own port.
  `k8s/roles/airflow/templates/networkpolicy-postgres.yaml.j2` admits the four Airflow apps
  and observability's Grafana to 5432. A source a DAG reads needs an allow for
  `app: airflow-task` in its own fence, after a caller census (ADR 0009). Prometheus's is
  netpol-baseline's `prometheus-callers`. A grant for a source in another role is a PR of its
  own whenever this role's PR also changes its `containers_list` entry's edges: the
  render-diff gate refuses one PR that both adds a `dependsOn` edge onto a tag and changes
  that tag's render.
- **Backed up daily to R2.** The entry's `r2_backup_claims` puts the claim on the daily tier
  beside the home-critical volumes. The warehouse holds daily history its sources discard:
  Scrutiny keeps about six days and Home Assistant's recorder seven, so a week-old restore
  would lose days no backfill can re-derive. ADR 0007 records the exception.

## Every DAG follows these rules

- **One partition per day, replaced in one transaction**
  (`k8s/roles/airflow/files/dags/homelab_warehouse/warehouse.py:replace_partition`). A rerun
  or a backfill is always safe.
- **Each load creates its schema through `ensure_schema`**, which takes an advisory lock
  first: concurrent `GRANT`s on one catalog row fail with "tuple concurrently updated".
- **A `check` task after each `load` reads the day back.** It fails the run on a missing key,
  an hour-long gap or a value out of bounds.
- **A day the source no longer wholly holds is skipped, not loaded.** Prometheus' 7GB size
  cap binds before its 30-day time cap, so
  `k8s/roles/airflow/files/dags/homelab_warehouse/prom_rollup.py:oldest_sample` reads the
  oldest sample live.
- **Transforms stay stdlib-only and Airflow-free**, so the role's tests import them without
  Airflow. Those tests run against recorded source responses in
  `k8s/roles/airflow/tests/fixtures/`.
- **A source is read by ClusterIP, never through a route.** A task pod retries a refused
  connection, because kube-router admits a new pod a few seconds late.
- **A new DAG, not a new task, for a new rollup.** Airflow creates no task instance for a task
  added to runs that already succeeded, so a new task in an old DAG loads only from its first
  new run. A new DAG's `catchup` backfills every day the source holds.

## The DAGs

- **`prom_daily_rollup`** loads `prom.ups_energy_daily` and `prom.cpu_temp_daily`, whose
  columns are shaped to their one metric.
- **`prom_series_rollup`** loads `prom.series_daily`, one row per day, metric and key, with the
  day's min, mean, max and 95th percentile. Each metric is a `Rollup` row in
  `k8s/roles/airflow/files/dags/homelab_warehouse/prom_series.py:ROLLUPS`, which also sets
  what its `check` holds it to. A new Prometheus metric is a new row there and needs no DDL.
- **The volume rows are filesystem usage of mounted claims**, from kubelet, not Longhorn's
  `actualSize` (ADR 0006). Kubelet reports only a claim a running pod mounts, so a CronJob's
  claim gaps legitimately: the `volume_*` metrics check a floor on the row count and no gap.
- **`ha_daily_rollup`** loads `ha.sensor_daily`, one row per day and declared sensor in
  `k8s/roles/airflow/files/dags/homelab_warehouse/ha_rollup.py:SENSORS`. HA writes a state when
  it changes, so the mean is weighted by how long each value held, and time spent
  `unavailable` or `unknown` stands in for a gap. The recorder keeps seven days
  (`purge_keep_days`), so this DAG's catchup reaches back six, and an outage of the DAG longer
  than that loses days for good.
  `k8s/roles/airflow/tests/test_ha_rollup.py::test_the_backfill_window_matches_the_recorders_purge_setting`
  holds the window to HA's setting. Task pods reach HA through home-assistant's `netpol_from`
  entry in `cluster/services.yml`.
- **`claude_usage_rollup`** loads `claude.usage_daily`: spend, tokens, sessions, active time,
  lines, commits and PRs, summed by node, entrypoint, model and type, so no session id or
  account reaches the warehouse. Claude Code exports one cumulative counter per session, so
  `k8s/roles/airflow/files/dags/homelab_warehouse/claude_usage.py:usage_rows` counts each
  session's rise from its last value before the day, never PromQL's `increase()`, which drops
  every new session's first export. The series merge `without (instance)`, because a
  restarted collector re-exports each live session under a new `instance`, and the baseline
  looks back eight days, past the collector's 168h `metric_expiration`. A day loads only
  once Prometheus holds those eight days, so the DAG backfills about two weeks, not three.
- **`claude_decisions_rollup`** loads `claude.decision_daily` and `claude.tool_result_daily`
  from observability's Loki, which its task pods reach through netpol-baseline's
  `loki-callers`. LogQL metric queries give exact counts; each `user_*` decision is
  classified from its session's raw events as `prompted`, `machine`, `remembered` or
  `unresolved`, and a `config` or `hook` decision is a `rule` or `hook`. The load refuses a
  day where the classified decisions do not add up to the counts, and halves any raw page
  that comes back full. The rules and their measured thresholds are in
  `k8s/roles/airflow/files/dags/homelab_warehouse/claude_decisions.py:classify`.
  `k8s/roles/airflow/tests/test_claude_decisions.py::test_the_backfill_window_stays_a_day_inside_lokis_retention`
  holds `LOKI_KEEP_DAYS` to `observability_loki_retention`. The query keeps only the fields
  the classification reads, so no command, prompt or session id leaves Loki.
- **`edge_daily_rollup`** loads `edge.counts_daily`: Traefik's requests per entrypoint and
  per route by status code, CrowdSec's scenario overflows, AppSec requests and blocks and the
  bouncer's LAPI requests, and the day's highest active-decision count per origin. Each counter
  series counts its rise from its last value in the day before, and a drop counts as a
  restart (`k8s/roles/airflow/files/dags/homelab_warehouse/prom_rollup.py:series_increase`).
  Every label stays in the series' identity: the CrowdSec agent runs inside the Traefik pod,
  so each Traefik restart starts its counters under a new `instance`. The step is one minute,
  because a restart loses the rise between the last sample before it and the reset.
  `check` fails a day with fewer than
  `k8s/roles/airflow/files/dags/homelab_warehouse/edge_counts.py:MIN_DAILY_REQUESTS` Traefik
  requests or no bouncer requests, since both run all day.
- **`backup_spend_rollup`** loads `longhorn.backup_daily`: per volume and day, the backups
  Longhorn ran, the blocks each walked and the blocks it uploaded. Each walked block is one
  HeadObject, so `class_b` is the backup's Class B transaction count. The lines come from
  loki-homelab, which task pods reach through netpol-baseline's `loki-homelab` fence, and the
  catchup reaches back 30 days. A volume's claim and backup target come from its Longhorn
  Volume object, read when the day loads, so a volume moved between tiers counts wholly
  under its new target. B2's target is named `default`.
- **`smart_daily_rollup`** loads `smart.attribute_daily`, one row per day, disk and SMART
  attribute, from Scrutiny by ClusterIP through scrutiny's `netpol_from` entry. Scrutiny keeps a
  reading for each day only for about the last
  `k8s/roles/airflow/files/dags/homelab_warehouse/smart_history.py:DAILY_DAYS` days, then
  points days apart, then one a month. So a day's row is the latest reading Scrutiny holds
  dated that day, a day it no longer holds gets no row, and `check` requires every active
  disk only inside the daily window. The catchup from 2026-07-01 loaded the sparse older
  points once.
- **`dns_daily_rollup`** loads `dns.counts_daily` from both Pi-hole instances' query
  databases, which keep 91 days: each day's total, blocked and client counts, every client's
  queries, and the top
  `k8s/roles/airflow/files/dags/homelab_warehouse/dns_history.py:TOP_DOMAINS` permitted and
  blocked domains, per instance. Task pods reach the API through netpol-baseline's `pihole`
  fence. Pi-hole v6 has no read-only credential, so only the `load` task holds the admin
  password, read from `pihole-env` by its `executor_config`, and every session it opens is
  logged out, because Pi-hole caps open sessions.
- **`github_daily_rollup`** loads `github.counts_daily`: merged PRs per author with their
  time to merge, Actions runs per workflow and conclusion with their run time, and findings
  issues opened and closed. The Actions API returns at most 1,000 runs for one filtered
  query, so runs are read an hour at a time and a fuller window is halved. Every token of one
  user shares one quota, so the DAG runs one day at a time at 02:00, and `load` raises to retry
  later when fewer than
  `k8s/roles/airflow/files/dags/homelab_warehouse/github_history.py:MIN_REMAINING` calls are
  left. Only `load` holds `airflow_github_token`, through its `executor_config`.
- **Only `backup_spend_rollup`'s `load` task holds a Kubernetes token.** Its
  `executor_config` runs it as `airflow-longhorn-reader`, whose one grant is `list` on
  Longhorn volumes (`k8s/roles/airflow/templates/rbac.yaml.j2`). Every other task pod runs as
  `airflow-task`, which mounts no token.
- **Grafana's "Homelab history" board**
  (`k8s/roles/observability/files/dashboards/Infrastructure/homelab-history.json`) reads every
  DAG's tables through the `Warehouse` datasource.

## Operating it

- **To rerun a day**, clear its run in the UI. `catchup` already queues every day since
  `start_date` that the source still holds, at most four at once.
- **A run's ID names the day after the one it loads.** `scheduled__2026-11-09T00:00:00+00:00`
  is the run whose `logical_date`, and so whose data day, is 2026-11-08.
- **Read a task's state from Airflow, never from Loki.** The operator reads a run's task
  states with `airflow tasks states-for-dag-run <dag_id> <run_id>`, run by `kubectl exec`
  in the `airflow-scheduler` container, or in the UI's Runs tab.
- **A task's log lives in loki-homelab, not in Airflow.** The task's supervisor writes every
  log line, the exception and its frames included, to the pod's stdout as JSON. The scheduler
  deletes the pod as soon as it exits, and the UI's log view reads only the pod's own file, so
  the view is empty for a finished task. A task that fails still exits 0, so its pod goes too;
  `AIRFLOW__KUBERNETES_EXECUTOR__DELETE_WORKER_PODS_ON_FAILURE` keeps only a pod whose
  container crashed.
- **Every task pod lingers 15 seconds after its task exits.** Alloy publishes discovered pods
  at most once every 5 seconds and re-globs their log files every 10. Kubelet removes a
  pod's log file when the pod is deleted, so a pod gone before Alloy opens its file leaves
  nothing in Loki. The `base` container's command in
  `k8s/roles/airflow/templates/pod-template.yaml.j2` runs the image's entrypoint, sleeps,
  then exits with the task's status
  (`tests/services/test_airflow_task_log_linger.py::test_the_task_pod_outlives_alloys_file_sync_after_the_task_exits`,
  `tests/services/test_airflow_task_log_linger.py::test_the_executors_args_reach_the_entrypoint_and_its_status_is_the_pods`).
- **To read one task attempt's log in Grafana**, open Explore on the `loki-homelab`
  datasource. A task's pod is named for its DAG and task, with `_` written as `-`, and each
  attempt has its own pod. First find the attempt's pod: its `Executing workload` line names
  the run and the attempt in its `log_path`.

  ```logql
  {namespace="homelab", container="base", pod=~"dns-daily-rollup-load-.+"}
    |= "run_id=scheduled__2026-11-12T01:25:00+00:00/task_id=load/attempt=2.log"
  ```

  Then read that pod's whole stream, which holds the entrypoint's and the supervisor's lines
  as well as the task's: `{namespace="homelab", pod="dns-daily-rollup-load-8a92tku8"}`.
  Loki keeps it for `loki_homelab_retention`
  (`k8s/roles/loki-homelab/defaults/main.yml:loki_homelab_retention`). A filter such as
  `| json | run_id="…"` drops the lines that carry no `run_id`.
- **The Postgres init script runs only on an empty data directory.** To rotate
  `airflow_postgres_password`, `airflow_warehouse_writer_password` or
  `airflow_warehouse_reader_password`, the operator runs `ALTER ROLE … PASSWORD` in the pod
  after the new value deploys. Nothing else changes the stored hash.
- **`airflow_ha_token` is a Home Assistant long-lived access token.** To rotate it, the
  operator makes a new one in HA's UI under their profile's Security tab, sets it with
  `sops set`, runs `cluster_vars.py write`, and deletes the old token in HA once the new one
  deploys.
- **`airflow_github_token` is a fine-grained GitHub token** with read-only access to this
  repo's Actions, issues, metadata and pull requests. It expires; to rotate it, the operator
  makes a new one with the same access, sets it with `sops set`, runs `cluster_vars.py write`,
  and deletes the old one on GitHub once the new one deploys.
- **A warehouse DAG that misses a day pages through Healthchecks.io.** Each DAG's `check`
  ends in `k8s/roles/airflow/files/dags/homelab_warehouse/warehouse.py:finish_check`, which
  fails the task on a problem or records the passed day in `meta.check_log`. At 06:00 UTC
  `warehouse_freshness` pings the `airflow-warehouse-fresh` check, or its `/fail` naming each
  DAG with no passed record for yesterday, and Healthchecks.io also alerts when no ping
  comes. A new warehouse DAG joins
  `k8s/roles/airflow/files/dags/homelab_warehouse/freshness.py:WAREHOUSE_DAGS`;
  `k8s/roles/airflow/tests/test_freshness.py::test_every_dag_that_records_its_check_is_watched`
  fails until it does. To find what missed a day, read the ping's body in the Healthchecks.io
  console, then the DAG's runs in Airflow.
- **A dead scheduler pages as `AirflowSchedulerDown`.** monitor-bridge's `airflow_scheduler`
  row reads the scheduler's own health server every cycle, and goes down on a 503 or when the
  server does not answer (`k8s/roles/monitor-bridge/files/checks/service.py:check_airflow_scheduler`).
  The rule waits 10 minutes, so the weekly reboot does not page.

## Editing

- DAGs: `k8s/roles/airflow/files/dags/`. A new file in the build context is a key under
  `image_builder_builds` in `defaults/main.yml` and a `cp` in the Dockerfile.
- Shared env, mounts and the migration wait: `k8s/roles/airflow/templates/airflow-env.yaml.j2`,
  macros only.
- Image pins, resources, the claim and the Secret: `k8s/roles/airflow/defaults/main.yml`.
