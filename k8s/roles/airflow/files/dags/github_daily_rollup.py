"""Daily merged PRs, Actions runs and findings issues, from GitHub's REST API.

One run per UTC day, at 02:00, and one at a time, so the catchup spreads its API calls. `load`
replaces the day's rows in `github.counts_daily`; `check` reads them back and fails the run on
a day with no Actions run. Only `load` holds the read-only token, through its
`executor_config`. Every token of one user shares one quota, so `load` waits for the next
window when fewer than `MIN_REMAINING` calls are left.
"""

import os
from datetime import datetime, timedelta, timezone

from airflow.providers.postgres.hooks.postgres import PostgresHook
from airflow.sdk import Asset, dag, task
from airflow.timetables.interval import CronDataIntervalTimetable
from homelab_warehouse import github_history as gh
from homelab_warehouse.warehouse import (
    check_day,
    ensure_schema,
    replace_partition,
)
from kubernetes.client import models as k8s

WAREHOUSE_CONN = "warehouse"
GITHUB_COUNTS_DAILY = Asset("warehouse://github/counts_daily")
GITHUB_TOKEN = {
    "pod_override": k8s.V1Pod(
        spec=k8s.V1PodSpec(
            containers=[
                k8s.V1Container(
                    name="base",
                    env=[
                        k8s.V1EnvVar(
                            name="GITHUB_TOKEN",
                            value_from=k8s.V1EnvVarSource(
                                secret_key_ref=k8s.V1SecretKeySelector(
                                    name="airflow", key="GITHUB_TOKEN"
                                )
                            ),
                        )
                    ],
                )
            ]
        )
    )
}


def _day(data_interval_start):
    return data_interval_start.astimezone(timezone.utc).date()


@dag(
    dag_id="github_daily_rollup",
    schedule=CronDataIntervalTimetable("0 2 * * *", timezone="UTC"),
    start_date=datetime(2026, 10, 20, tzinfo=timezone.utc),
    catchup=True,
    max_active_runs=1,
    default_args={"retries": 6, "retry_delay": timedelta(minutes=30)},
    tags=["warehouse", "github"],
)
def github_daily_rollup():
    @task(outlets=[GITHUB_COUNTS_DAILY], executor_config=GITHUB_TOKEN)
    def load(data_interval_start=None) -> dict:
        day = _day(data_interval_start)
        api = gh.GitHub(os.environ["GITHUB_TOKEN"])
        remaining = api.remaining()
        if remaining < gh.MIN_REMAINING:
            raise RuntimeError(
                f"{remaining} GitHub calls left, under {gh.MIN_REMAINING}; retrying later"
            )
        rows = gh.day_rows(day, api.get)
        conn = PostgresHook(postgres_conn_id=WAREHOUSE_CONN).get_conn()
        try:
            with conn.cursor() as cursor:
                ensure_schema(cursor, gh.SCHEMA, gh.DDL)
            conn.commit()
            inserted = replace_partition(
                conn, gh.COUNTS_TABLE, "day", day, gh.COUNTS_COLUMNS, rows
            )
        finally:
            conn.close()
        return {"rows": inserted}

    @task
    def check(data_interval_start=None) -> None:
        day = _day(data_interval_start)
        check_day(
            PostgresHook(postgres_conn_id=WAREHOUSE_CONN).get_conn,
            "github_daily_rollup",
            day,
            [gh.COUNTS_TABLE],
            gh.quality_problems,
        )

    load() >> check()


github_daily_rollup()
