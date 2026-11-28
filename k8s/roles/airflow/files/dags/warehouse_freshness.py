"""Each morning, page through Healthchecks.io when a warehouse DAG did not pass yesterday.

Runs at 06:00 UTC, after every load and check is due. Only its task holds the Healthchecks.io
ping key, through its `executor_config`, from the `airflow` Secret.
"""

import os
from datetime import datetime, timedelta, timezone

from airflow.providers.postgres.hooks.postgres import PostgresHook
from airflow.sdk import dag, task
from homelab_warehouse import freshness as fr
from kubernetes.client import models as k8s

WAREHOUSE_CONN = "warehouse"
HC_PING_KEY = {
    "pod_override": k8s.V1Pod(
        spec=k8s.V1PodSpec(
            containers=[
                k8s.V1Container(
                    name="base",
                    env=[
                        k8s.V1EnvVar(
                            name="HC_PING_KEY",
                            value_from=k8s.V1EnvVarSource(
                                secret_key_ref=k8s.V1SecretKeySelector(
                                    name="airflow", key="HC_PING_KEY"
                                )
                            ),
                        )
                    ],
                )
            ]
        )
    )
}


@dag(
    dag_id="warehouse_freshness",
    schedule=fr.HC_SCHEDULE,
    start_date=datetime(2026, 11, 20, tzinfo=timezone.utc),
    catchup=False,
    default_args={"retries": 3, "retry_delay": timedelta(minutes=5)},
    tags=["warehouse"],
)
def warehouse_freshness() -> None:
    @task(executor_config=HC_PING_KEY)
    def report() -> list[str]:
        conn = PostgresHook(postgres_conn_id=WAREHOUSE_CONN).get_conn()
        try:
            newest = fr.newest_checked_days(conn)
        finally:
            conn.close()
        stale = fr.stale_dags(newest, datetime.now(timezone.utc).date())
        fr.ping(os.environ["HC_PING_KEY"], stale)
        return stale

    report()


warehouse_freshness()
