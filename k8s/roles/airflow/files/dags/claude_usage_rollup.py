"""Daily Claude Code spend, tokens, sessions, active time and output, from Prometheus.

One run per UTC day. `load` replaces the day's rows in `claude.usage_daily`, summed by node,
entrypoint, model and type; `check` reads them back and fails the run on a negative row, an
implausible total, or spend and tokens that disagree. A day is loaded only once Prometheus
holds the eight days before it, which each session's baseline is read from.
"""

from datetime import datetime, timedelta, timezone

from airflow.providers.postgres.hooks.postgres import PostgresHook
from airflow.sdk import Asset, dag, task
from airflow.sdk.exceptions import AirflowSkipException
from airflow.timetables.interval import CronDataIntervalTimetable
from homelab_warehouse import claude_usage as cu
from homelab_warehouse import prom_rollup as pr
from homelab_warehouse.warehouse import (
    check_day,
    ensure_schema,
    interval_day,
    replace_partition,
)

WAREHOUSE_CONN = "warehouse"
CLAUDE_USAGE_DAILY = Asset("warehouse://claude/usage_daily")


@dag(
    dag_id="claude_usage_rollup",
    schedule=CronDataIntervalTimetable("45 0 * * *", timezone="UTC"),
    start_date=datetime(2026, 10, 20, tzinfo=timezone.utc),
    catchup=True,
    max_active_runs=4,
    default_args={"retries": 2, "retry_delay": timedelta(minutes=10)},
    tags=["warehouse", "claude-code"],
)
def claude_usage_rollup() -> None:
    @task(outlets=[CLAUDE_USAGE_DAILY])
    def load(data_interval_start: datetime | None = None) -> dict:
        day = interval_day(data_interval_start)
        oldest = pr.oldest_sample(pr.PROM_URL)
        if not pr.in_backfill_window(
            day, datetime.now(timezone.utc), oldest + cu.BASELINE_SPAN
        ):
            raise AirflowSkipException(
                f"{day}: Prometheus holds samples from {oldest} only, and a baseline "
                f"needs {cu.BASELINE_SPAN.days} days before the day"
            )
        window = pr.DayWindow.for_day(day)
        rows = []
        for metric in cu.METRICS:
            result = pr.fetch_range(
                pr.PROM_URL,
                cu.usage_query(metric, window),
                window,
                step_s=cu.STEP_S,
                through_midnight=True,
            )
            baselines = pr.baselines_by_series(
                pr.fetch_instant(pr.PROM_URL, cu.baseline_query(metric, window))
            )
            rows += cu.usage_rows(metric, window, result, baselines)
        conn = PostgresHook(postgres_conn_id=WAREHOUSE_CONN).get_conn()
        try:
            with conn.cursor() as cursor:
                ensure_schema(cursor, cu.SCHEMA, cu.DDL)
            conn.commit()
            inserted = replace_partition(
                conn, cu.USAGE_TABLE, "day", day, cu.USAGE_COLUMNS, rows
            )
        finally:
            conn.close()
        return {"rows": inserted}

    @task
    def check(data_interval_start: datetime | None = None) -> None:
        day = interval_day(data_interval_start)
        check_day(
            PostgresHook(postgres_conn_id=WAREHOUSE_CONN).get_conn,
            "claude_usage_rollup",
            day,
            [cu.USAGE_TABLE],
            cu.quality_problems,
        )

    load() >> check()


claude_usage_rollup()
