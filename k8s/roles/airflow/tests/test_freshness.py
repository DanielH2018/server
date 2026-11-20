"""The freshness report: which DAGs missed yesterday, what it pings, and which DAGs it covers."""

import re
import urllib.request
from datetime import date
from pathlib import Path

from dags.homelab_warehouse import freshness as fr

TODAY = date(2026, 11, 21)
YESTERDAY = date(2026, 11, 20)
DAGS = Path(__file__).parents[1] / "files/dags"


def test_a_dag_that_passed_yesterday_is_fresh_and_one_that_did_not_is_named():
    newest = {"prom_daily_rollup": YESTERDAY, "ha_daily_rollup": date(2026, 11, 17)}

    stale = fr.stale_dags(
        newest, TODAY, ["prom_daily_rollup", "ha_daily_rollup", "dns_daily_rollup"]
    )

    assert stale == [
        "ha_daily_rollup: newest passed day 2026-11-17",
        "dns_daily_rollup: newest passed day none",
    ]


def test_a_fresh_warehouse_pings_success_and_a_stale_one_pings_fail():
    sent = []

    def opener(request: urllib.request.Request, timeout):
        sent.append((request.full_url, request.get_method(), request.data))
        return open("/dev/null", "rb")

    fr.ping("k3y", [], opener=opener)
    fr.ping("k3y", ["ha_daily_rollup: newest passed day none"], opener=opener)

    assert sent == [
        (
            "https://hc-ping.com/k3y/airflow-warehouse-fresh",
            "POST",
            b"every warehouse DAG passed yesterday",
        ),
        (
            "https://hc-ping.com/k3y/airflow-warehouse-fresh/fail",
            "POST",
            b"ha_daily_rollup: newest passed day none",
        ),
    ]


def test_every_dag_that_records_its_check_is_watched():
    recording = {
        dag_id
        for path in DAGS.glob("*.py")
        if "finish_check(" in path.read_text()
        for dag_id in re.findall(r'dag_id="([a-z_]+)"', path.read_text())
    }

    assert {"prom_daily_rollup", "github_daily_rollup"} <= recording
    assert recording == set(fr.WAREHOUSE_DAGS)
