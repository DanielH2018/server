"""Daily host load and memory, GPU busy, UPS battery and volume usage, from Prometheus.

One run per UTC day. `load` replaces the day's rows in `prom.series_daily`, one per metric
and key; `check` reads them back and fails the run on a missing key, an hour-long gap or a
value out of bounds, as each metric's `Rollup` defines them. A day Prometheus no longer wholly
holds is skipped.
"""

from datetime import datetime, timedelta, timezone

from airflow.providers.postgres.hooks.postgres import PostgresHook
from airflow.sdk import Asset, dag, task
from airflow.sdk.exceptions import AirflowSkipException
from airflow.timetables.interval import CronDataIntervalTimetable
from homelab_warehouse import prom_rollup as pr
from homelab_warehouse import prom_series as ps
from homelab_warehouse.warehouse import (
    check_day,
    ensure_schema,
    replace_partition,
)

WAREHOUSE_CONN = "warehouse"
PROM_SERIES = Asset("warehouse://prom/series_daily")


def _day(data_interval_start):
    return data_interval_start.astimezone(timezone.utc).date()


@dag(
    dag_id="prom_series_rollup",
    schedule=CronDataIntervalTimetable("15 0 * * *", timezone="UTC"),
    start_date=datetime(2026, 10, 20, tzinfo=timezone.utc),
    catchup=True,
    max_active_runs=4,
    default_args={"retries": 2, "retry_delay": timedelta(minutes=10)},
    tags=["warehouse", "prometheus"],
)
def prom_series_rollup():
    @task(outlets=[PROM_SERIES])
    def load(data_interval_start=None) -> dict:
        day = _day(data_interval_start)
        oldest = pr.oldest_sample(pr.PROM_URL)
        if not pr.in_backfill_window(day, datetime.now(timezone.utc), oldest):
            raise AirflowSkipException(
                f"{day}: Prometheus holds samples from {oldest} only"
            )
        window = pr.DayWindow.for_day(day)
        rows = []
        for rollup in ps.ROLLUPS:
            rows += ps.series_rows(
                rollup, window, pr.fetch_range(pr.PROM_URL, rollup.query, window)
            )
        conn = PostgresHook(postgres_conn_id=WAREHOUSE_CONN).get_conn()
        try:
            with conn.cursor() as cursor:
                ensure_schema(cursor, pr.SCHEMA, ps.SERIES_DDL)
            conn.commit()
            inserted = replace_partition(
                conn, ps.SERIES_TABLE, "day", day, ps.SERIES_COLUMNS, rows
            )
        finally:
            conn.close()
        return {"rows": inserted}

    @task
    def check(data_interval_start=None) -> None:
        day = _day(data_interval_start)
        check_day(
            PostgresHook(postgres_conn_id=WAREHOUSE_CONN).get_conn,
            "prom_series_rollup",
            day,
            [ps.SERIES_TABLE],
            ps.series_problems,
        )

    load() >> check()


prom_series_rollup()
