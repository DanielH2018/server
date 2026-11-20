"""The Uptime Robot console against the monitors the doc names.

The console is read through a `FakeSources` and the cache is a fresh dict per test, so nothing
here patches the module.
"""

from dataclasses import replace

from _fake_sources import FakeSources
from bridge.streaks import ProbeCache
from bridge.types import CheckResult
import checks.uptime_robot as ur
from diagnostics.probe_lib.uptime_robot import documented_monitors

EXPECTED = (
    {"id": "803868101", "name": "Auth Health"},
    {"id": "803868270", "name": "Jellyfin Health"},
)

# v2 `getMonitors` as the read-only key gets it: integer ids, status 2 is up.
MONITORS = [
    {"id": 803868101, "friendly_name": "Auth Health", "status": 2},
    {"id": 803868270, "friendly_name": "Jellyfin Health", "status": 2},
]


def _answer(monitors, **fields):
    body = {
        "stat": "ok",
        "pagination": {"offset": 0, "limit": 50, "total": len(monitors)},
        "monitors": monitors,
        **fields,
    }
    return lambda url, payload: body


def _armed(cfg):
    return replace(cfg, UPTIME_ROBOT_API_KEY="k", UPTIME_ROBOT_MONITORS=EXPECTED)


def _fresh_probe() -> ProbeCache:
    return {"ts": None, "ok": True, "msg": ""}


def test_every_documented_monitor_running_is_clean(cfg):
    src = FakeSources(post_json=_answer(MONITORS))
    ok, msg = ur.uptime_robot_monitors(_armed(cfg), src, probe=_fresh_probe())
    assert ok and "2 documented Uptime Robot monitors" in msg
    assert src.calls == [
        ("post_json", (cfg.UPTIME_ROBOT_API_URL, {"api_key": "k", "format": "json"}))
    ]


def test_a_deleted_monitor_is_flagged_by_name(cfg):
    src = FakeSources(post_json=_answer(MONITORS[1:]))
    result = CheckResult.of(
        ur.uptime_robot_monitors(_armed(cfg), src, probe=_fresh_probe())
    )
    assert not result.ok
    assert "Auth Health (803868101): no such monitor" in result.msg
    assert result.details == ("Auth Health",)


def test_a_paused_monitor_is_flagged_and_a_down_one_is_not(cfg):
    monitors = [{**MONITORS[0], "status": 9}, {**MONITORS[1], "status": 0}]
    src = FakeSources(post_json=_answer(monitors))
    result = CheckResult.of(
        ur.uptime_robot_monitors(_armed(cfg), src, probe=_fresh_probe())
    )
    assert not result.ok
    assert result.details == ("Jellyfin Health",)
    assert "Jellyfin Health (803868270): paused" in result.msg


def test_a_refused_request_is_down_and_reprobed_next_cycle(cfg):
    refused = {"stat": "fail", "error": {"type": "invalid_parameter"}}
    src = FakeSources(post_json=lambda url, payload: refused)
    probe = _fresh_probe()
    ok, msg = ur.uptime_robot_monitors(_armed(cfg), src, now=1000.0, probe=probe)
    assert not ok and "unverified" in msg and "invalid_parameter" in msg
    assert not ur.uptime_robot_monitors(_armed(cfg), src, now=1300.0, probe=probe)[0]
    assert len(src.calls) == 2


def test_a_partial_page_is_down_rather_than_a_missing_monitor(cfg):
    src = FakeSources(
        post_json=_answer(
            MONITORS[:1], pagination={"offset": 0, "limit": 1, "total": 2}
        )
    )
    ok, msg = ur.uptime_robot_monitors(_armed(cfg), src, probe=_fresh_probe())
    assert not ok and "1 of 2 monitors" in msg and "no such monitor" not in msg


def test_a_success_is_cached_for_the_interval(cfg):
    cfg = _armed(cfg)
    src = FakeSources(post_json=_answer(MONITORS))
    probe = _fresh_probe()
    assert ur.uptime_robot_monitors(cfg, src, now=1000.0, probe=probe)[0]
    ok, msg = ur.uptime_robot_monitors(
        cfg, src, now=1000.0 + cfg.UPTIME_ROBOT_PROBE_INTERVAL_S - 1, probe=probe
    )
    assert ok and "checked" in msg
    assert len(src.calls) == 1


def test_disabled_without_a_key(cfg):
    ok, msg = ur.uptime_robot_monitors(cfg, FakeSources(), probe=_fresh_probe())
    assert ok and "disabled" in msg


def test_the_rendered_monitor_list_is_the_docs_table(cfg):
    assert cfg.UPTIME_ROBOT_MONITORS
    assert [(m["id"], m["name"]) for m in cfg.UPTIME_ROBOT_MONITORS] == [
        (row["id"], row["name"]) for row in documented_monitors()
    ]
