"""Daily Home Assistant sensor history: air quality, temperatures and plug power and energy.

One run per UTC day. `load` replaces the day's rows in `ha.sensor_daily`, one per declared
sensor; `check` reads them back and fails the run on a missing sensor, a long outage or a value
out of bounds. The recorder keeps seven days, so a day older than that is skipped, and an
outage of the DAG longer than that loses days for good.
"""

import os
from datetime import datetime, timedelta, timezone

from airflow.providers.postgres.hooks.postgres import PostgresHook
from airflow.sdk import Asset, dag, task
from airflow.sdk.exceptions import AirflowSkipException
from airflow.timetables.interval import CronDataIntervalTimetable
from homelab_warehouse import ha_rollup as ha
from homelab_warehouse.warehouse import (
    ensure_schema,
    finish_check,
    read_partition,
    replace_partition,
)

WAREHOUSE_CONN = "warehouse"
HA_SENSOR_DAILY = Asset("warehouse://ha/sensor_daily")


def _day(data_interval_start):
    return data_interval_start.astimezone(timezone.utc).date()


@dag(
    dag_id="ha_daily_rollup",
    schedule=CronDataIntervalTimetable("30 0 * * *", timezone="UTC"),
    start_date=datetime(2026, 11, 12, tzinfo=timezone.utc),
    catchup=True,
    max_active_runs=4,
    default_args={"retries": 2, "retry_delay": timedelta(minutes=10)},
    tags=["warehouse", "home-assistant"],
)
def ha_daily_rollup():
    @task(outlets=[HA_SENSOR_DAILY])
    def load(data_interval_start=None) -> dict:
        day = _day(data_interval_start)
        if not ha.in_backfill_window(day, datetime.now(timezone.utc)):
            raise AirflowSkipException(
                f"{day}: the recorder keeps only {ha.HA_KEEP_DAYS} days"
            )
        history = ha.fetch_history(
            ha.HA_URL,
            os.environ["HA_TOKEN"],
            day,
            [sensor.entity_id for sensor in ha.SENSORS],
        )
        rows = ha.sensor_rows(day, history)
        conn = PostgresHook(postgres_conn_id=WAREHOUSE_CONN).get_conn()
        try:
            with conn.cursor() as cursor:
                ensure_schema(cursor, ha.SCHEMA, ha.DDL)
            conn.commit()
            inserted = replace_partition(
                conn, ha.SENSOR_TABLE, "day", day, ha.SENSOR_COLUMNS, rows
            )
        finally:
            conn.close()
        return {"rows": inserted}

    @task
    def check(data_interval_start=None) -> None:
        day = _day(data_interval_start)
        conn = PostgresHook(postgres_conn_id=WAREHOUSE_CONN).get_conn()
        try:
            rows = read_partition(conn, ha.SENSOR_TABLE, "day", day)
        finally:
            conn.close()
        problems = ha.quality_problems(day, rows)
        finish_check(
            PostgresHook(postgres_conn_id=WAREHOUSE_CONN).get_conn,
            "ha_daily_rollup",
            day,
            problems,
        )

    load() >> check()


ha_daily_rollup()
