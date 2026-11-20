"""Daily SMART readings per disk, from Scrutiny.

One run per UTC day. `load` replaces the day's rows in `smart.attribute_daily` with the last
reading Scrutiny holds for each active device that day; `check` reads them back and fails the
run on a recent day missing a device or a temperature out of bounds. The catchup from
2026-07-01 loads whichever older days Scrutiny's downsampled history still holds.
"""

from datetime import datetime, timedelta, timezone

from airflow.providers.postgres.hooks.postgres import PostgresHook
from airflow.sdk import Asset, dag, task
from airflow.sdk.exceptions import AirflowSkipException
from airflow.timetables.interval import CronDataIntervalTimetable
from homelab_warehouse import smart_history as sh
from homelab_warehouse.warehouse import (
    ensure_schema,
    finish_check,
    read_partition,
    replace_partition,
)

WAREHOUSE_CONN = "warehouse"
SMART_DAILY = Asset("warehouse://smart/attribute_daily")


def _day(data_interval_start):
    return data_interval_start.astimezone(timezone.utc).date()


@dag(
    dag_id="smart_daily_rollup",
    schedule=CronDataIntervalTimetable("15 1 * * *", timezone="UTC"),
    start_date=datetime(2026, 7, 1, tzinfo=timezone.utc),
    catchup=True,
    max_active_runs=4,
    default_args={"retries": 2, "retry_delay": timedelta(minutes=10)},
    tags=["warehouse", "smart"],
)
def smart_daily_rollup():
    @task(outlets=[SMART_DAILY])
    def load(data_interval_start=None) -> dict:
        day = _day(data_interval_start)
        if not sh.is_complete(day, datetime.now(timezone.utc)):
            raise AirflowSkipException(f"{day} has not ended")
        devices = sh.active_devices(sh.fetch_summary())
        details = {wwn: sh.fetch_details(wwn) for wwn in devices}
        rows = sh.smart_rows(day, devices, details)
        conn = PostgresHook(postgres_conn_id=WAREHOUSE_CONN).get_conn()
        try:
            with conn.cursor() as cursor:
                ensure_schema(cursor, sh.SCHEMA, sh.DDL)
            conn.commit()
            inserted = replace_partition(
                conn, sh.SMART_TABLE, "day", day, sh.SMART_COLUMNS, rows
            )
        finally:
            conn.close()
        return {"rows": inserted}

    @task
    def check(data_interval_start=None) -> None:
        day = _day(data_interval_start)
        conn = PostgresHook(postgres_conn_id=WAREHOUSE_CONN).get_conn()
        try:
            rows = read_partition(conn, sh.SMART_TABLE, "day", day)
        finally:
            conn.close()
        devices = sh.active_devices(sh.fetch_summary())
        problems = sh.quality_problems(day, rows, devices, datetime.now(timezone.utc))
        finish_check(
            PostgresHook(postgres_conn_id=WAREHOUSE_CONN).get_conn,
            "smart_daily_rollup",
            day,
            problems,
        )

    load() >> check()


smart_daily_rollup()
