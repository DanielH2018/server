"""The Claude Code cgroup arm folded into check_mem (issue #1258).

PR #1251 (#1238) started scraping `claude_cgroup_*` for `claude-rc.service` and
`user.slice/user-1000.slice`, and nothing read them: no alert, no panel. What they expose is the
opening move of the 2026-09-05 incident (#1243) — the claude-rc cgroup stalled in memory reclaim
for about ten minutes before anything downstream failed.

Per this repo's red-proof rule every behaviour here is a PAIR: one input the arm must pass and
one it must flag. A check is only ever observed passing, and an arm that fires on everything is
indistinguishable from one that fires on nothing when you only look at the green side —
`volume-claim` shipped behind 16 passing tests and then fired for 0 of 25 claims.

`test_the_queries_name_metrics_and_labels_the_writer_actually_emits` in
`ansible/tests/services/` is the non-vacuity half: it pins the metric names and cgroup labels
these queries select against the script that writes them, so a rename on either side fails with a
name rather than quietly selecting nothing.

Run: uv run pytest ansible/roles/k8s/monitor-bridge/tests/test_check_claude_cgroups.py
"""

from dataclasses import replace

import pytest

import checks.host
from verdicts.host_cgroups import claude_cgroup_verdict


STALL_METRIC = "claude_cgroup_memory_pressure_stalled_usec_total"
EVENT_METRIC = "claude_cgroup_memory_events_total"

# The two cgroups the writer labels. `claude-rc` is the one whose absence is a fault;
# `user-1000-slice` exists only once somebody has logged in since boot.
RC = {"cgroup": "claude-rc"}
LOGIN = {"cgroup": "user-1000-slice"}

# What check_mem's own query answers in these tests: both hosts reporting, both healthy, so the
# memory verdict and _host_origin_shortfall never decide an outcome the arm is supposed to.
HEALTHY_MEMORY = [({"origin": "daniel-box"}, 30.0), ({"origin": "daniel-server"}, 41.0)]


@pytest.fixture
def armed(cfg):
    """A config with the arm switched on, as templates/env-secret.yaml.j2 switches it on."""
    return replace(cfg, CLAUDE_CGROUPS=("claude-rc",))


def _vectors(stalls, events, seen=None):
    """A fetch fake answering each of the arm's two queries separately.

    Handed to `check_mem` through its `prom_vector` seam rather than patched onto `bridge.net` —
    nothing here monkeypatches a first-party module, which is what
    `test_no_test_module_patches_more_modules_than_its_allowlist_entry` asks of a new test module.

    Dispatching on the metric each query names is the load-bearing part: one lambda would hand
    the stall vector back for the events query too, which is how a fixture ends up proving the
    opposite of what it claims. `check_mem`'s own node_memory_* query gets both hosts, healthy, so
    neither the memory verdict nor `_host_origin_shortfall` decides an outcome the arm is about.
    """

    def fake_vector(_cfg, promql):
        if seen is not None:
            seen.append(promql)
        if STALL_METRIC in promql:
            return stalls
        if EVENT_METRIC in promql:
            return events
        return HEALTHY_MEMORY

    return fake_vector


# ── the verdict, in isolation ────────────────────────────────────────────────────────────────


def test_a_quiet_cgroup_is_clean():
    ok, msg = claude_cgroup_verdict(
        [(RC, 0.0004)], [(RC | {"event": "oom"}, 0.0)], ["claude-rc"], 10, "5m", "10m"
    )
    assert ok
    assert "0.00%" in msg


def test_an_oom_kill_is_flagged():
    """Any increase is bad by definition — no baseline needed, which is why this arm shipped."""
    ok, msg = claude_cgroup_verdict(
        [(RC, 0.0004)],
        [(RC | {"event": "oom_kill"}, 1.0)],
        ["claude-rc"],
        10,
        "5m",
        "10m",
    )
    assert not ok
    assert "oom_kill" in msg
    assert "claude-rc" in msg


def test_a_stall_under_the_threshold_is_clean():
    ok, _ = claude_cgroup_verdict([(RC, 9.9)], [], ["claude-rc"], 10, "5m", "10m")
    assert ok


def test_a_stall_over_the_threshold_is_flagged():
    ok, msg = claude_cgroup_verdict([(LOGIN, 41.0)], [], ["claude-rc"], 10, "5m", "10m")
    assert not ok
    assert "user-1000-slice" in msg
    assert "41" in msg


def test_a_reporting_cgroup_is_clean_but_a_missing_one_is_flagged():
    """An empty vector must not read green — the kopia-board failure, one level up.

    `b2_billable_bytes` rendered nothing for two weeks behind a perfectly resolving datasource
    because nobody wrote the gauge any more. A check whose series vanish has exactly that shape.
    """
    assert claude_cgroup_verdict([(RC, 0.1)], [], ["claude-rc"], 10, "5m", "10m")[0]
    ok, msg = claude_cgroup_verdict([(LOGIN, 0.1)], [], ["claude-rc"], 10, "5m", "10m")
    assert not ok
    assert "claude-rc" in msg
    assert "UNKNOWN" in msg


def test_an_event_outranks_a_stall_in_the_message():
    """A kill is the thing the stall was warning about, so it leads."""
    ok, msg = claude_cgroup_verdict(
        [(RC, 80.0)],
        [(RC | {"event": "oom_kill"}, 2.0)],
        ["claude-rc"],
        10,
        "5m",
        "10m",
    )
    assert not ok
    assert msg.startswith("claude cgroup memory events")


# ── the arm, folded into check_mem ───────────────────────────────────────────────────────────


def test_the_arm_is_off_until_a_cgroup_is_configured(cfg):
    """Empty CLAUDE_CGROUPS disables it, like PI_GLANCES_URL — and no query is issued.

    This is also what keeps the rest of test_check_host.py honest: those tests answer every query
    with one memory-percentage vector, and an arm that queried on the default config would read
    that vector as a stall rate.
    """
    queries = []
    ok, msg = checks.host.check_mem(cfg, _vectors([], [], queries))
    assert ok
    assert "claude" not in msg
    assert not any(STALL_METRIC in q or EVENT_METRIC in q for q in queries)


def test_a_quiet_arm_reports_alongside_the_memory_verdict(armed):
    ok, msg = checks.host.check_mem(armed, _vectors([(RC, 0.0004)], []))
    assert ok
    assert "mem 41%" in msg
    assert "claude cgroups stalled" in msg


def test_a_firing_arm_leads_the_message_and_pages(armed):
    armed = replace(armed, CLAUDE_CGROUP_CONSECUTIVE=1)
    fetch = _vectors([(RC, 0.0)], [(RC | {"event": "oom_kill"}, 1.0)])
    ok, msg = checks.host.check_mem(armed, fetch)
    assert not ok
    assert msg.startswith("claude cgroup memory events")
    assert "mem 41%" in msg


def test_hysteresis_holds_the_first_cycle_then_pages(armed):
    """CLAUDE_CGROUP_CONSECUTIVE=2 is ten minutes at INTERVAL=300 — the incident's own duration.

    It also absorbs the weekly claude-rc restart, which recreates the cgroup and zeroes every
    counter: Prometheus extrapolates across a reset, so one `increase()` window spanning one can
    report a rise from a counter that went 0 -> 0.
    """
    breaching = _vectors([(RC, 55.0)], [])
    assert checks.host.check_mem(armed, breaching)[0] is True
    assert checks.host.check_mem(armed, breaching)[0] is False


def test_a_clean_cycle_resets_the_streak(armed):
    breaching = _vectors([(RC, 55.0)], [])
    quiet = _vectors([(RC, 0.1)], [])
    assert checks.host.check_mem(armed, breaching)[0] is True
    assert checks.host.check_mem(armed, quiet)[0] is True
    assert checks.host.check_mem(armed, breaching)[0] is True


def test_the_event_set_pages_on_kills_and_not_on_high(armed):
    """`high` is excluded on purpose, and it is the one exclusion worth pinning.

    MemoryHigh throttling is the cap doing its job, and `high` is keyed to a value another role
    owns and is actively changing (#1264 moves claude_code_rc_memory_high / _swap_max). Every
    event kept means the same thing whatever those caps become. `high` is graphed instead.
    """
    watched = set(armed.CLAUDE_CGROUP_EVENTS.split("|"))
    assert watched == {"max", "oom", "oom_kill", "oom_group_kill"}
    assert "high" not in watched
    assert "low" not in watched


def test_the_queries_are_instant_not_subqueries(armed):
    """The measured query must be the shipped query — the PR #482 trap.

    A `[6h:1m]` subquery derived the threshold and is far more expensive than what runs every
    cycle. Both shipped queries were timed against the live Prometheus three times each on
    2026-09-06 at 0.125-0.139s end to end.
    """
    queries = []
    checks.host.check_mem(armed, _vectors([(RC, 0.0)], [], queries))
    arm = [q for q in queries if STALL_METRIC in q or EVENT_METRIC in q]
    assert len(arm) == 2
    assert not any(":" in q.split("[")[-1] for q in arm), arm
