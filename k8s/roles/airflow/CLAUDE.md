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
- **Claim:** `airflow-postgres-data` (weekly -> B2 (default target))
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
- **Two additive fences.** `k8s/roles/airflow/templates/networkpolicy.yaml.j2` admits
  `airflow-task` and `airflow-dag-processor` to 8080, from the entry's `netpol_from`.
  `k8s/roles/airflow/templates/networkpolicy-postgres.yaml.j2` admits the four Airflow apps
  and observability's Grafana to 5432. A source a DAG reads needs an allow for
  `app: airflow-task` in its own fence, after a caller census (ADR 0009). Prometheus's is
  netpol-baseline's `prometheus-callers`. A grant for a source in another role is a PR of its
  own whenever this role's PR also changes its `containers_list` entry's edges: the
  render-diff gate refuses one PR that both adds a `dependsOn` edge onto a tag and changes
  that tag's render.
- **Backed up weekly to B2, on Tuesday's shard.** A lost week re-derives by backfill from
  sources that keep at least seven days. Home Assistant's recorder keeps exactly seven, so
  its slice depends on that week holding.

## Every DAG follows these rules

- **One partition per day, replaced in one transaction**
  (`k8s/roles/airflow/files/dags/homelab_warehouse/warehouse.py:replace_partition`). A rerun
  or a backfill is always safe.
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

## Operating it

- **To rerun a day**, clear its run in the UI. `catchup` already queues every day since
  `start_date` that the source still holds, at most four at once.
- **A task pod's stdout reaches loki-homelab.** Read a run's log with
  `uv run python scripts/diagnostics/probe.py loki-query --loki homelab '{namespace="homelab", pod=~"prom-daily-rollup-.*"}'`.
  The UI's log view reads only the pod's own file, which goes when the pod does (#9202).
- **The Postgres init script runs only on an empty data directory.** To rotate
  `airflow_postgres_password`, `airflow_warehouse_writer_password` or
  `airflow_warehouse_reader_password`, the operator runs `ALTER ROLE … PASSWORD` in the pod
  after the new value deploys. Nothing else changes the stored hash.
- **Nothing pages on a dead scheduler yet.** The heartbeat deadman is the milestone's next
  PR (#9201). Until it lands, a stopped scheduler shows only as runs that never start.

## Editing

- DAGs: `k8s/roles/airflow/files/dags/`. A new file in the build context is a key under
  `image_builder_builds` in `defaults/main.yml` and a `cp` in the Dockerfile.
- Shared env, mounts and the migration wait: `k8s/roles/airflow/templates/airflow-env.yaml.j2`,
  macros only.
- Image pins, resources, the claim and the Secret: `k8s/roles/airflow/defaults/main.yml`.
