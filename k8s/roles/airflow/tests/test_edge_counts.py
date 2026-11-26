"""The edge rollup's counter arithmetic and its check, against a recorded day.

`fixtures/edge-2026-11-19.json` holds two counters' range results over 2026-11-19 UTC at the
rollup's step, each with its `baseline_query` result, and `decisions_query`'s result. Traefik
restarted three times that day. `entrypoint_requests` keeps the one series of https GET 200s,
and `crowdsec_overflows` keeps the `crowdsecurity/http-probing` scenario across the four
CrowdSec agents that ran in Traefik's pods that day.
"""

import json
from datetime import date
from pathlib import Path

import pytest

from dags.homelab_warehouse import edge_counts as ec
from dags.homelab_warehouse import prom_rollup as pr

DAY = date(2026, 11, 19)
WINDOW = pr.DayWindow.for_day(DAY)
RECORDED = json.loads(
    (Path(__file__).parent / "fixtures/edge-2026-11-19.json").read_text()
)
BY_METRIC = {counter.metric: counter for counter in ec.COUNTERS}


def rows_for(metric: str) -> dict[tuple[str, str], float]:
    recorded = RECORDED[metric]
    rows = ec.counter_rows(
        BY_METRIC[metric],
        WINDOW,
        recorded["range"],
        pr.baselines_by_series(recorded["baseline"]),
    )
    return {(row[2], row[3]): row[4] for row in rows}


def day_rows(**totals: float) -> list[dict]:
    return [
        {"metric": metric, "name": "", "detail": "", "value": value}
        for metric, value in totals.items()
    ]


def test_a_counter_that_resets_with_its_pod_counts_each_climb_again():
    assert rows_for("entrypoint_requests") == {
        ("https", "200"): (3491 - 3439) + 10241 + 589 + 44611
    }


def test_an_agent_the_baseline_does_not_hold_counts_from_zero():
    # The agent that ran before the day ends flat at its baseline of 40; the three agents
    # started within the day end at 137, 40 and 351.
    assert rows_for("crowdsec_overflows") == {
        ("crowdsecurity/http-probing", ""): 137 + 40 + 351
    }


def test_a_route_is_named_without_its_provider_and_crd_hash():
    assert ec.route_name("homelab-deploy-ui-1b0289dc4311bacb00d5@kubernetescrd") == (
        "homelab-deploy-ui"
    )
    assert ec.route_name("livesync-couchdb@file") == "livesync-couchdb"


def test_the_decision_rows_keep_each_origins_highest_count():
    rows = ec.decision_rows(WINDOW, RECORDED["decisions"])

    assert {row[2]: row[4] for row in rows} == {
        "CAPI": 18973,
        "crowdsec": 37,
        "cscli": 1,
    }


def test_a_day_with_edge_traffic_passes_the_check():
    rows = day_rows(entrypoint_requests=133_489, bouncer_requests=2862)

    assert ec.quality_problems(DAY, rows) == []


@pytest.mark.parametrize(
    ("totals", "problem"),
    [
        (
            {"entrypoint_requests": 12, "bouncer_requests": 2862},
            "Traefik served 12 requests, under 1000",
        ),
        (
            {"entrypoint_requests": 133_489, "bouncer_requests": 0},
            "the bouncer made no LAPI requests",
        ),
        (
            {
                "entrypoint_requests": 133_489,
                "bouncer_requests": 2862,
                "appsec_blocks": -1,
            },
            "appsec_blocks has a negative row",
        ),
        (
            {"entrypoint_requests": 2e8, "bouncer_requests": 2862},
            "entrypoint_requests sums to 200000000.0, over 100000000.0",
        ),
    ],
)
def test_a_lost_or_implausible_day_fails_the_check(totals, problem):
    assert ec.quality_problems(DAY, day_rows(**totals)) == [f"{DAY}: {problem}"]
