"""The Claude Code cgroup arm judges each session host's writer separately (#4870).

Both `has_claude_code` hosts, daniel-box and daniel-server, report the same cgroup names. An arm
that asks only "is `claude-rc` reporting from anywhere" reads one host's dead writer as green,
because the other host still reports the name.

These tests drive `checks.host.check_mem` rather than `claude_cgroup_verdict` directly. The
verdict has no input naming the expected hosts yet, and `check_mem(cfg, src)` reaches whatever
the fix adds through the rendered `cfg` fixture. Each test runs several cycles on one
`FakeSources` and judges the last, so the result does not depend on how many cycles of grace the
fix gives a missing host.

Run: uv run pytest ansible/roles/k8s/monitor-bridge/tests/test_check_claude_cgroup_origins.py
"""

from dataclasses import replace

import checks.host
from _fake_sources import FakeSources


STALL_METRIC = "claude_cgroup_memory_pressure_stalled_usec_total"
EVENT_METRIC = "claude_cgroup_memory_events_total"
UID_METRIC = "claude_uid_processes"

# check_mem's own node_memory_* query: both hosts reporting and healthy, so neither the memory
# verdict nor its host-coverage floor is what names daniel-server.
HEALTHY_MEMORY = [({"origin": "daniel-box"}, 30.0), ({"origin": "daniel-server"}, 41.0)]

# More cycles than any streak the arm carries today (CLAUDE_CGROUP_CONSECUTIVE=2,
# HOST_ORIGINS_CONSECUTIVE=3) needs to page.
CYCLES = 8


def _stall(origin: str, cgroup: str) -> tuple[dict, float]:
    return {"origin": origin, "cgroup": cgroup}, 0.01


def _last_cycle(cfg, stalls) -> tuple[bool, str]:
    """`check_mem`'s verdict after CYCLES cycles in which the arm's queries answer `stalls`."""

    def fake_vector(promql):
        if STALL_METRIC in promql:
            return list(stalls)
        if EVENT_METRIC in promql or UID_METRIC in promql:
            return []
        return HEALTHY_MEMORY

    src = FakeSources(prom_vector=fake_vector)
    result = (True, "")
    for _ in range(CYCLES):
        result = checks.host.check_mem(cfg, src)
    return result


def test_a_host_whose_writer_reports_nothing_is_flagged(cfg):
    """The Verify-by case: daniel-box reports claude-rc and fleet, daniel-server reports nothing."""
    assert set(cfg.CLAUDE_CGROUPS) == {"claude-rc", "fleet"}
    cfg = replace(cfg, CLAUDE_CGROUP_CONSECUTIVE=1)
    stalls = [_stall("daniel-box", "claude-rc"), _stall("daniel-box", "fleet")]
    ok, msg = _last_cycle(cfg, stalls)
    assert not ok, msg
    assert "daniel-server" in msg


def test_a_cgroup_missing_on_one_host_is_flagged_and_named(cfg):
    """The missing set is per host: daniel-server lacking claude-rc pages though daniel-box has it."""
    cfg = replace(cfg, CLAUDE_CGROUP_CONSECUTIVE=1)
    stalls = [
        _stall("daniel-box", "claude-rc"),
        _stall("daniel-box", "fleet"),
        _stall("daniel-server", "fleet"),
    ]
    ok, msg = _last_cycle(cfg, stalls)
    assert not ok, msg
    assert "daniel-server" in msg
    assert "claude-rc" in msg


def test_claude_rc_missing_where_its_unit_runs_is_flagged(cfg):
    """daniel-box runs claude-rc.service, so its claude-rc going silent pages and names it.

    The replacement the PR #4874 review proposed for the test above: same per-host shape, on
    the host whose rendered expectation includes claude-rc.
    """
    cfg = replace(cfg, CLAUDE_CGROUP_CONSECUTIVE=1)
    stalls = [_stall("daniel-box", "fleet"), _stall("daniel-server", "fleet")]
    ok, msg = _last_cycle(cfg, stalls)
    assert not ok, msg
    assert "daniel-box/claude-rc" in msg
    assert "daniel-server" not in msg.split("|")[0]


def test_the_live_steady_state_reads_green(cfg):
    """daniel-server sets claude_code_rc_enabled: false, so its lack of claude-rc is healthy.

    The cgroups live Prometheus reported on 2026-10-13: claude-rc and fleet from daniel-box,
    fleet alone from daniel-server.
    """
    cfg = replace(cfg, CLAUDE_CGROUP_CONSECUTIVE=1)
    stalls = [
        _stall("daniel-box", "claude-rc"),
        _stall("daniel-box", "fleet"),
        _stall("daniel-server", "fleet"),
    ]
    ok, msg = _last_cycle(cfg, stalls)
    assert ok, msg
    assert "UNKNOWN" not in msg
