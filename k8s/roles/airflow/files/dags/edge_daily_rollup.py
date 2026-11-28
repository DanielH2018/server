"""Daily Traefik request counts and CrowdSec detections, from Prometheus.

One run per UTC day. `load` replaces the day's rows in `edge.counts_daily`; `check` reads them
back and fails the run on a negative row, an implausible total, or a day with no Traefik or
bouncer traffic. A day is loaded only once Prometheus holds the day before it, which each
series' baseline is read from.
"""

from datetime import datetime, timedelta, timezone

from airflow.providers.postgres.hooks.postgres import PostgresHook
from airflow.sdk import Asset, dag, task
from airflow.sdk.exceptions import AirflowSkipException
from airflow.timetables.interval import CronDataIntervalTimetable
from homelab_warehouse import edge_counts as ec
from homelab_warehouse import prom_rollup as pr
from homelab_warehouse.warehouse import (
    check_day,
    ensure_schema,
    interval_day,
    replace_partition,
)

WAREHOUSE_CONN = "warehouse"
EDGE_COUNTS_DAILY = Asset("warehouse://edge/counts_daily")


@dag(
    dag_id="edge_daily_rollup",
    schedule=CronDataIntervalTimetable("55 0 * * *", timezone="UTC"),
    start_date=datetime(2026, 10, 25, tzinfo=timezone.utc),
    catchup=True,
    max_active_runs=4,
    default_args={"retries": 2, "retry_delay": timedelta(minutes=10)},
    tags=["warehouse", "edge"],
)
def edge_daily_rollup() -> None:
    @task(outlets=[EDGE_COUNTS_DAILY])
    def load(data_interval_start: datetime | None = None) -> dict:
        day = interval_day(data_interval_start)
        oldest = pr.oldest_sample(pr.PROM_URL)
        if not pr.in_backfill_window(
            day, datetime.now(timezone.utc), oldest + ec.BASELINE_SPAN
        ):
            raise AirflowSkipException(
                f"{day}: Prometheus holds samples from {oldest} only, and a baseline "
                "needs the day before"
            )
        window = pr.DayWindow.for_day(day)
        rows = []
        for counter in ec.COUNTERS:
            result = pr.fetch_range(
                pr.PROM_URL,
                counter.series,
                window,
                step_s=ec.STEP_S,
                through_midnight=True,
            )
            baselines = pr.baselines_by_series(
                pr.fetch_instant(pr.PROM_URL, ec.baseline_query(counter, window))
            )
            rows += ec.counter_rows(counter, window, result, baselines)
        rows += ec.decision_rows(
            window, pr.fetch_instant(pr.PROM_URL, ec.decisions_query(window))
        )
        conn = PostgresHook(postgres_conn_id=WAREHOUSE_CONN).get_conn()
        try:
            with conn.cursor() as cursor:
                ensure_schema(cursor, ec.SCHEMA, ec.DDL)
            conn.commit()
            inserted = replace_partition(
                conn, ec.COUNTS_TABLE, "day", day, ec.COUNTS_COLUMNS, rows
            )
        finally:
            conn.close()
        return {"rows": inserted}

    @task
    def check(data_interval_start: datetime | None = None) -> None:
        day = interval_day(data_interval_start)
        check_day(
            PostgresHook(postgres_conn_id=WAREHOUSE_CONN).get_conn,
            "edge_daily_rollup",
            day,
            [ec.COUNTS_TABLE],
            ec.quality_problems,
        )

    load() >> check()


edge_daily_rollup()
