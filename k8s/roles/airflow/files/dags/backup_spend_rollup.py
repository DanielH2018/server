"""Daily Longhorn backup counts and their B2 Class B transactions, from loki-homelab.

One run per UTC day. `load` replaces the day's rows in `longhorn.backup_daily`, one per volume
that backed up, with its claim and backup target from the Longhorn Volume object; `check`
reads them back and fails the run on a day with no backup line. Only `load` runs as
`airflow-longhorn-reader`, the one task identity with a Kubernetes token.
"""

from datetime import datetime, timedelta, timezone

from airflow.providers.postgres.hooks.postgres import PostgresHook
from airflow.sdk import Asset, dag, task
from airflow.sdk.exceptions import AirflowSkipException
from airflow.timetables.interval import CronDataIntervalTimetable
from homelab_warehouse import backup_spend as bs
from homelab_warehouse.warehouse import (
    check_day,
    ensure_schema,
    replace_partition,
)
from kubernetes.client import models as k8s

WAREHOUSE_CONN = "warehouse"
BACKUP_DAILY = Asset("warehouse://longhorn/backup_daily")
LONGHORN_READER = {
    "pod_override": k8s.V1Pod(
        spec=k8s.V1PodSpec(
            service_account_name="airflow-longhorn-reader",
            automount_service_account_token=True,
            containers=[k8s.V1Container(name="base")],
        )
    )
}


def _day(data_interval_start):
    return data_interval_start.astimezone(timezone.utc).date()


@dag(
    dag_id="backup_spend_rollup",
    schedule=CronDataIntervalTimetable("5 1 * * *", timezone="UTC"),
    start_date=datetime(2026, 10, 20, tzinfo=timezone.utc),
    catchup=True,
    max_active_runs=4,
    default_args={"retries": 2, "retry_delay": timedelta(minutes=10)},
    tags=["warehouse", "longhorn"],
)
def backup_spend_rollup():
    @task(outlets=[BACKUP_DAILY], executor_config=LONGHORN_READER)
    def load(data_interval_start=None) -> dict:
        day = _day(data_interval_start)
        if not bs.in_backfill_window(day, datetime.now(timezone.utc)):
            raise AirflowSkipException(
                f"{day}: loki-homelab no longer holds the whole day"
            )
        lines = bs.fetch_backup_lines(bs.LOKI_URL, day)
        rows = bs.backup_rows(day, lines, bs.volumes_by_name(bs.fetch_volumes()))
        conn = PostgresHook(postgres_conn_id=WAREHOUSE_CONN).get_conn()
        try:
            with conn.cursor() as cursor:
                ensure_schema(cursor, bs.SCHEMA, bs.DDL)
            conn.commit()
            inserted = replace_partition(
                conn, bs.BACKUP_TABLE, "day", day, bs.BACKUP_COLUMNS, rows
            )
        finally:
            conn.close()
        return {"rows": inserted}

    @task
    def check(data_interval_start=None) -> None:
        day = _day(data_interval_start)
        check_day(
            PostgresHook(postgres_conn_id=WAREHOUSE_CONN).get_conn,
            "backup_spend_rollup",
            day,
            [bs.BACKUP_TABLE],
            bs.quality_problems,
        )

    load() >> check()


backup_spend_rollup()
