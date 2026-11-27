"""Daily DNS query counts per Pi-hole instance, client and domain, from FTL's database.

One run per UTC day. `load` replaces the day's rows in `dns.counts_daily` for both instances;
`check` reads them back and fails the run on an instance that answered almost nothing or whose
clients add up to more than its total. Only `load` holds the Pi-hole admin password, read
from `pihole-env` through its `executor_config`, because Pi-hole has no read-only credential.
"""

import os
from datetime import datetime, timedelta, timezone

from airflow.providers.postgres.hooks.postgres import PostgresHook
from airflow.sdk import Asset, dag, task
from airflow.sdk.exceptions import AirflowSkipException
from airflow.timetables.interval import CronDataIntervalTimetable
from homelab_warehouse import dns_history as dh
from homelab_warehouse.warehouse import (
    check_day,
    ensure_schema,
    replace_partition,
)
from kubernetes.client import models as k8s

WAREHOUSE_CONN = "warehouse"
DNS_COUNTS_DAILY = Asset("warehouse://dns/counts_daily")
PIHOLE_PASSWORD = {
    "pod_override": k8s.V1Pod(
        spec=k8s.V1PodSpec(
            containers=[
                k8s.V1Container(
                    name="base",
                    env=[
                        k8s.V1EnvVar(
                            name="PIHOLE_PASSWORD",
                            value_from=k8s.V1EnvVarSource(
                                secret_key_ref=k8s.V1SecretKeySelector(
                                    name="pihole-env",
                                    key="FTLCONF_webserver_api_password",
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
    dag_id="dns_daily_rollup",
    schedule=CronDataIntervalTimetable("25 1 * * *", timezone="UTC"),
    start_date=datetime(2026, 8, 20, tzinfo=timezone.utc),
    catchup=True,
    max_active_runs=4,
    default_args={"retries": 2, "retry_delay": timedelta(minutes=10)},
    tags=["warehouse", "dns"],
)
def dns_daily_rollup():
    @task(outlets=[DNS_COUNTS_DAILY], executor_config=PIHOLE_PASSWORD)
    def load(data_interval_start=None) -> dict:
        day = _day(data_interval_start)
        if not dh.in_backfill_window(day, datetime.now(timezone.utc)):
            raise AirflowSkipException(f"{day}: Pi-hole no longer holds the whole day")
        rows = []
        for instance, url in dh.INSTANCES.items():
            with dh.PiholeSession(url, os.environ["PIHOLE_PASSWORD"]) as pihole:
                rows += dh.instance_rows(day, instance, pihole.get)
        conn = PostgresHook(postgres_conn_id=WAREHOUSE_CONN).get_conn()
        try:
            with conn.cursor() as cursor:
                ensure_schema(cursor, dh.SCHEMA, dh.DDL)
            conn.commit()
            inserted = replace_partition(
                conn, dh.COUNTS_TABLE, "day", day, dh.COUNTS_COLUMNS, rows
            )
        finally:
            conn.close()
        return {"rows": inserted}

    @task
    def check(data_interval_start=None) -> None:
        day = _day(data_interval_start)
        check_day(
            PostgresHook(postgres_conn_id=WAREHOUSE_CONN).get_conn,
            "dns_daily_rollup",
            day,
            [dh.COUNTS_TABLE],
            dh.quality_problems,
        )

    load() >> check()


dns_daily_rollup()
