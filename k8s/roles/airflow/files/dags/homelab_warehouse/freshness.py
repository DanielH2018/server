"""Whether every warehouse DAG's check passed for yesterday, reported to Healthchecks.io.

A failed nightly load shows only in Airflow's UI, and several sources keep days, not months:
Scrutiny's daily readings about six, Home Assistant seven. A failure nobody sees for a week
loses those days for good. So `warehouse_freshness` reads `meta.check_log` each morning and
pings one off-site Healthchecks.io check: success when every DAG's check passed for
yesterday, `/fail` naming each one that did not. When nothing pings at all, because the
scheduler or this DAG is down, Healthchecks.io alerts on the silence.

Stdlib only, so the repo's tests import it without Airflow.

Typical usage example:

    stale = stale_dags(newest_checked_days(conn), today)
    ping(ping_key, stale)
"""

import urllib.request
from collections.abc import Callable, Mapping, Sequence
from datetime import date, timedelta

from .warehouse import CHECK_TABLE

# Every DAG that runs its check through `warehouse.check_day`.
WAREHOUSE_DAGS = (
    "backup_spend_rollup",
    "claude_decisions_rollup",
    "claude_usage_rollup",
    "dns_daily_rollup",
    "edge_daily_rollup",
    "github_daily_rollup",
    "ha_daily_rollup",
    "prom_daily_rollup",
    "prom_series_rollup",
    "smart_daily_rollup",
)
HC_SLUG = "airflow-warehouse-fresh"
# The DAG's cron, UTC; monitor-bridge's console expectation for HC_SLUG is held to it.
HC_SCHEDULE = "0 6 * * *"
HC_URL = "https://hc-ping.com"


def newest_checked_days(conn) -> dict[str, date]:
    """Each DAG's newest day whose check passed, from `meta.check_log`."""
    cursor = conn.cursor()
    try:
        cursor.execute(f"SELECT dag_id, max(day) FROM {CHECK_TABLE} GROUP BY dag_id")
        return dict(cursor.fetchall())
    finally:
        cursor.close()


def stale_dags(
    newest: Mapping[str, date], today: date, dags: Sequence[str] = WAREHOUSE_DAGS
) -> list[str]:
    """Each DAG, with its newest passed day, whose check has not passed for yesterday."""
    yesterday = today - timedelta(days=1)
    return [
        f"{dag}: newest passed day {newest.get(dag, 'none')}"
        for dag in dags
        if newest.get(dag, date.min) < yesterday
    ]


def ping(
    ping_key: str, stale: Sequence[str], opener: Callable = urllib.request.urlopen
) -> None:
    """Report to Healthchecks.io: success when `stale` is empty, else `/fail` listing it."""
    url = f"{HC_URL}/{ping_key}/{HC_SLUG}" + ("/fail" if stale else "")
    body = ("\n".join(stale) or "every warehouse DAG passed yesterday").encode()
    with opener(urllib.request.Request(url, data=body, method="POST"), timeout=30):
        pass
