"""H5.3's compose condition on the landing's host wait.

A merge that changes the Pi's files under `/usr/lib/homelab/compose/` also needs a passing
`homelab_compose_up_result` at the merge SHA or a descendant. The helpers come from
`test_land_host_wait.py`, which owns the Prometheus stub and the wait driver.

Run: uv run pytest scripts/deploy_tools/tests/test_land_compose_wait.py
"""

from dataclasses import replace
from pathlib import Path

from deploy_tools.land_lib import compose_wait, host_wait
from test_land_host_wait import (
    MERGE,
    PARENT,
    _at,
    _pi,
    _prom,
    _reads,
    _series,
    _snapshots,
    _wait,
)

COMPOSE_FILE = f"{compose_wait.COMPOSE_ROOT}compose.yaml"


def _up(sha: str, value: str = "1") -> dict:
    labels = {"host": "daniel-pi", "sha": sha}
    return {compose_wait.UP_RESULT: [{"metric": labels, "value": [0, value]}]}


def _settled_host(compose_series: dict) -> dict:
    return _series() | compose_series


def _compose_pi():
    return replace(_pi(), compose=True)


def test_a_change_under_the_compose_directory_is_flagged_and_one_elsewhere_is_not():
    old = {COMPOSE_FILE: ("file", 0o644, b"a"), "other": ("file", 0o644, b"x")}
    assert compose_wait.touches_compose(
        old, {**old, COMPOSE_FILE: ("file", 0o644, b"b")}
    )
    assert compose_wait.touches_compose({}, old)
    assert not compose_wait.touches_compose(
        old, {**old, "other": ("file", 0o644, b"y")}
    )


def test_host_targets_marks_only_a_compose_change(tmp_path):
    snapshot, _ = _snapshots(tmp_path, {MERGE: ["daniel-pi"], PARENT: ["daniel-pi"]})
    old = {COMPOSE_FILE: ("file", 0o644, b"a"), "other": ("file", 0o644, b"x")}
    for new, expected in (
        ({**old, COMPOSE_FILE: ("file", 0o644, b"b")}, True),
        ({**old, "other": ("file", 0o644, b"y")}, False),
    ):
        trees = {("a", "daniel-pi"): new, ("b", "daniel-pi"): old}
        (target,) = host_wait.host_targets(Path("/p"), MERGE, _reads(snapshot, trees))
        assert target.compose is expected


def test_a_passing_run_at_the_merge_sha_settles():
    fetch, _ = _prom(_settled_host(_up(MERGE[:12])))
    outcome, sleeps = _wait(_compose_pi(), fetch, [_at(12, 0)])
    assert outcome is None and sleeps == []


def test_a_failed_run_at_the_merge_sha_is_not_settled():
    fetch, _ = _prom(_settled_host(_up(MERGE[:12], value="0")))
    outcome, _ = _wait(_compose_pi(), fetch, [_at(12, 0), _at(12, 30), _at(12, 32)])
    assert (outcome.verdict, outcome.rc) == ("deferred", 75)


def test_a_passing_run_at_an_older_sha_keeps_waiting():
    fetch, _ = _prom(_settled_host(_up("c" * 12)))
    outcome, sleeps = _wait(
        _compose_pi(), fetch, [_at(12, 0), _at(12, 30), _at(12, 32)]
    )
    assert outcome.verdict == "deferred" and len(sleeps) == 2


def test_a_target_without_the_condition_ignores_the_compose_series():
    fetch, _ = _prom(_settled_host(_up("c" * 12, value="0")))
    outcome, _ = _wait(_pi(), fetch, [_at(12, 0)])
    assert outcome is None
