"""The bouncer-prune cron's decision: which rows `cscli bouncers prune` removes, and when a run
refuses because the prune could delete the last row holding the edge's key (`files/bouncer_prune.py`,
issue #2762).

Run: uv run pytest ansible/roles/k8s/crowdsec/tests/test_bouncer_prune.py
"""

import json
import os
import sys
from datetime import UTC, datetime, timedelta

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "files")
)

import bouncer_prune as bp

NOW = datetime(2026, 9, 27, 15, 0, tzinfo=UTC)


def _ts(age):
    """A timestamp `age` before NOW, in Go's RFC3339Nano shape as `cscli -o json` prints it."""
    return (NOW - age).strftime("%Y-%m-%dT%H:%M:%S.123456789Z")


def _row(name, pulled=None, created=timedelta(days=60)):
    return {
        "name": name,
        "last_pull": _ts(pulled) if pulled is not None else None,
        "created_at": _ts(created),
    }


LIVE = _row("k8straefik@10.42.0.207", pulled=timedelta(seconds=40))
# The shape observed on 2026-09-27: a base row last pulled in August, rows for past pod IPs,
# and the retired Docker bouncer that never pulled.
OBSERVED = [
    _row("k8straefik", pulled=timedelta(days=49)),
    _row("k8straefik@10.42.0.12", pulled=timedelta(days=3)),
    _row("k8straefik@10.42.0.150", pulled=timedelta(hours=2)),
    LIVE,
    _row("dockertraefik"),
]


def test_stale_rows_are_pruned_and_the_live_row_is_kept():
    decided = bp.plan(OBSERVED, NOW)
    assert not decided.refused
    assert decided.prune == [
        "dockertraefik",
        "k8straefik",
        "k8straefik@10.42.0.12",
        "k8straefik@10.42.0.150",
    ]
    assert decided.survivor is LIVE


def test_a_row_pulled_inside_the_window_is_kept():
    # The previous pod's row, 30 minutes after a restart, is still inside the hour.
    rows = [LIVE, _row("k8straefik@10.42.0.150", pulled=timedelta(minutes=30))]
    assert bp.plan(rows, NOW).prune == []


def test_a_row_that_never_pulled_is_judged_by_its_creation_time():
    # QueryBouncersInactiveSince falls back to created_at, so a fresh row that has not pulled
    # yet survives the prune and an old one does not.
    rows = [
        LIVE,
        _row("fresh", created=timedelta(minutes=5)),
        _row("dockertraefik", created=timedelta(days=45)),
    ]
    assert bp.plan(rows, NOW).prune == ["dockertraefik"]


def test_a_prune_with_no_recently_pulling_edge_row_is_refused():
    # A Traefik outage longer than the window: every k8straefik row would go, and with them
    # the key, so LAPI would answer the next pull with 403.
    rows = [
        _row("k8straefik", pulled=timedelta(days=49)),
        _row("k8straefik@10.42.0.207", pulled=timedelta(minutes=90)),
    ]
    decided = bp.plan(rows, NOW)
    assert decided.refused.startswith("refusing to prune")
    assert f"newest {_ts(timedelta(minutes=90))}" in decided.refused


def test_an_edge_row_pulled_inside_the_margin_is_refused():
    # 55 minutes is inside the hour but not inside hour-minus-margin: the seconds between the
    # list and the prune must not be able to age the only survivor out.
    rows = [_row("k8straefik@10.42.0.207", pulled=timedelta(minutes=55))]
    assert bp.plan(rows, NOW).refused


def test_a_list_with_no_edge_row_is_refused():
    assert bp.plan([_row("dockertraefik")], NOW).refused


def test_another_bouncers_fresh_pull_does_not_count_as_the_edge_survivor():
    rows = [
        _row("k8straefik@10.42.0.207", pulled=timedelta(hours=3)),
        _row("k8straefikx", pulled=timedelta(seconds=10)),
    ]
    assert bp.plan(rows, NOW).refused


def test_summary_caps_the_listed_names():
    rows = [LIVE] + [
        _row(f"k8straefik@10.42.1.{i}", pulled=timedelta(days=1)) for i in range(25)
    ]
    summary = bp.plan(rows, NOW).summary()
    assert summary.startswith("pruned 25 rows: ")
    assert "and 15 more" in summary
    assert summary.endswith(
        f"kept k8straefik@10.42.0.207 (last pull {LIVE['last_pull']})"
    )


def test_main_runs_the_prune_only_when_the_plan_is_safe(capsys):
    calls = []

    def runner(argv):
        calls.append(argv[argv.index("cscli") + 1 :])
        return json.dumps(OBSERVED) if argv[-1] == "json" else ""

    assert bp.main(runner, now=NOW) == 0
    assert calls == [
        ["bouncers", "list", "-o", "json"],
        ["bouncers", "prune", "-d", "60m", "--force"],
    ]
    assert capsys.readouterr().out.startswith("pruned 4 rows")


def test_main_refuses_without_pruning(capsys):
    calls = []
    stale = [_row("k8straefik@10.42.0.207", pulled=timedelta(hours=3))]

    def runner(argv):
        calls.append(argv[argv.index("cscli") + 1 :])
        return json.dumps(stale)

    assert bp.main(runner, now=NOW) == 1
    assert calls == [["bouncers", "list", "-o", "json"]]
    assert capsys.readouterr().out.startswith("refusing to prune")
