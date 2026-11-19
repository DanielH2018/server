"""Daily UPS energy and CPU package temperature, rolled up from Prometheus into the warehouse.

One run per UTC day. `load` replaces the day's rows in `prom.ups_energy_daily` and
`prom.cpu_temp_daily`; `check` reads them back and fails the run on a missing key, an
hour-long gap or a value out of bounds. A day Prometheus no longer wholly holds is skipped, so
catchup reaches back only as far as the source still holds.
"""

from datetime import datetime, timedelta, timezone

from airflow.exceptions import AirflowSkipException
from airflow.providers.postgres.hooks.postgres import PostgresHook
from airflow.sdk import Asset, dag, task
from airflow.timetables.interval import CronDataIntervalTimetable
from homelab_warehouse import prom_rollup as pr
from homelab_warehouse.warehouse import ensure_schema, replace_partition
from psycopg2.extras import RealDictCursor

WAREHOUSE_CONN = "warehouse"
PROM_DAILY = Asset("warehouse://prom/daily")


def _day(data_interval_start):
    return data_interval_start.astimezone(timezone.utc).date()


@dag(
    dag_id="prom_daily_rollup",
    schedule=CronDataIntervalTimetable("0 0 * * *", timezone="UTC"),
    start_date=datetime(2026, 10, 20, tzinfo=timezone.utc),
    catchup=True,
    max_active_runs=4,
    default_args={"retries": 2, "retry_delay": timedelta(minutes=10)},
    tags=["warehouse", "prometheus"],
)
def prom_daily_rollup():
    @task(outlets=[PROM_DAILY])
    def load(data_interval_start=None) -> dict:
        day = _day(data_interval_start)
        oldest = pr.oldest_sample(pr.PROM_URL)
        if not pr.in_backfill_window(day, datetime.now(timezone.utc), oldest):
            raise AirflowSkipException(
                f"{day}: Prometheus holds samples from {oldest} only"
            )
        window = pr.DayWindow.for_day(day)
        ups = pr.ups_energy_rows(
            window,
            pr.fetch_range(pr.PROM_URL, pr.UPS_LOAD_QUERY, window),
            pr.fetch_range(pr.PROM_URL, pr.UPS_NOMINAL_QUERY, window),
        )
        temps = pr.cpu_temp_rows(
            window, pr.fetch_range(pr.PROM_URL, pr.CPU_TEMP_QUERY, window)
        )
        conn = PostgresHook(postgres_conn_id=WAREHOUSE_CONN).get_conn()
        try:
            with conn.cursor() as cursor:
                ensure_schema(cursor, pr.SCHEMA, pr.DDL)
            conn.commit()
            return {
                "ups_rows": replace_partition(
                    conn, pr.UPS_TABLE, "day", day, pr.UPS_COLUMNS, ups
                ),
                "temp_rows": replace_partition(
                    conn, pr.TEMP_TABLE, "day", day, pr.TEMP_COLUMNS, temps
                ),
            }
        finally:
            conn.close()

    @task
    def check(data_interval_start=None) -> None:
        day = _day(data_interval_start)
        conn = PostgresHook(postgres_conn_id=WAREHOUSE_CONN).get_conn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(f"SELECT * FROM {pr.UPS_TABLE} WHERE day = %s", (day,))
                ups = cursor.fetchall()
                cursor.execute(f"SELECT * FROM {pr.TEMP_TABLE} WHERE day = %s", (day,))
                temps = cursor.fetchall()
        finally:
            conn.close()
        problems = pr.quality_problems(day, ups, temps)
        if problems:
            raise ValueError("quality check failed:\n" + "\n".join(problems))

    load() >> check()


prom_daily_rollup()
