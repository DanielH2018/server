"""Daily Claude Code permission decisions and tool results, from observability's Loki.

One run per UTC day. `load` counts every decision and tool result with LogQL metric queries,
classifies each `user_*` decision from its session's raw events, refuses the day when the two
disagree, and replaces the day's rows in `claude.decision_daily` and
`claude.tool_result_daily`. `check` reads them back. A day Loki may already be compacting is
skipped.
"""

from datetime import datetime, timedelta, timezone

from airflow.providers.postgres.hooks.postgres import PostgresHook
from airflow.sdk import Asset, dag, task
from airflow.sdk.exceptions import AirflowSkipException
from airflow.timetables.interval import CronDataIntervalTimetable
from homelab_warehouse import claude_decisions as cd
from homelab_warehouse.warehouse import (
    check_day,
    ensure_schema,
    replace_partition,
)

WAREHOUSE_CONN = "warehouse"
CLAUDE_DECISIONS = Asset("warehouse://claude/decision_daily")


def _day(data_interval_start):
    return data_interval_start.astimezone(timezone.utc).date()


@dag(
    dag_id="claude_decisions_rollup",
    schedule=CronDataIntervalTimetable("50 0 * * *", timezone="UTC"),
    start_date=datetime(2026, 10, 20, tzinfo=timezone.utc),
    catchup=True,
    max_active_runs=4,
    default_args={"retries": 2, "retry_delay": timedelta(minutes=10)},
    tags=["warehouse", "claude-code"],
)
def claude_decisions_rollup():
    @task(outlets=[CLAUDE_DECISIONS])
    def load(data_interval_start=None) -> dict:
        day = _day(data_interval_start)
        if not cd.in_backfill_window(day, datetime.now(timezone.utc)):
            raise AirflowSkipException(
                f"{day}: Loki keeps {cd.LOKI_KEEP_DAYS} days, and the oldest may be compacting"
            )
        sessions = cd.user_sessions(cd.fetch_counts(cd.LOKI_URL, cd.USER_SESSIONS, day))
        events = cd.fetch_events(cd.LOKI_URL, sessions, day)
        decisions = cd.decision_rows(
            day, cd.fetch_counts(cd.LOKI_URL, cd.DECISION_COUNTS, day), events
        )
        results = cd.result_rows(
            day, cd.fetch_counts(cd.LOKI_URL, cd.RESULT_COUNTS, day)
        )
        conn = PostgresHook(postgres_conn_id=WAREHOUSE_CONN).get_conn()
        try:
            with conn.cursor() as cursor:
                ensure_schema(cursor, cd.SCHEMA, cd.DDL)
            conn.commit()
            return {
                "decisions": replace_partition(
                    conn, cd.DECISION_TABLE, "day", day, cd.DECISION_COLUMNS, decisions
                ),
                "results": replace_partition(
                    conn, cd.RESULT_TABLE, "day", day, cd.RESULT_COLUMNS, results
                ),
            }
        finally:
            conn.close()

    @task
    def check(data_interval_start=None) -> None:
        day = _day(data_interval_start)
        check_day(
            PostgresHook(postgres_conn_id=WAREHOUSE_CONN).get_conn,
            "claude_decisions_rollup",
            day,
            [cd.DECISION_TABLE, cd.RESULT_TABLE],
            cd.quality_problems,
        )

    load() >> check()


claude_decisions_rollup()
