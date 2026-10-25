"""Loki: the selector roster and the log-error arm.

A LogQL selector naming a label Alloy does not ship matches no stream and reports "no events"
forever. HA_BAN_SELECTOR shipped that way with app="home-assistant". A fail-open arm cannot tell
"nothing to report" from "wrong question", so the selector labels are checked against the
observed stream vocabulary rather than against the check's own verdict.
"""

import io
import json

from dataclasses import fields, replace

import pytest

import bridge.config
import bridge.net
import checks.logs

from _bridge_env import bridge_env
from _fake_sources import FakeSources
from _loki_streams import (
    LOKI_STREAM_LABELS,
    _alloy_relabel_targets,
    _selector_labels,
)

# ── HA ip_ban arm ───────────────────────────────────────────────────────────────────────────
# HA's ban middleware keys on the peer address, so a burst of bad /api/ calls can ban the node's
# pod-network gateway. The probes exec curl to 127.0.0.1 and are immune; this arm is what
# keeps the ban itself from being silent.
# Two limits, so this guard is not over-trusted:
#   1. It reads each selector off the `cfg` fixture, which since #3659 is the rendered
#      pod environment, so the deployed LOG_ERROR_SELECTOR is what is checked. A selector built at runtime
#      from other values is not.
#   2. The vocabulary in ansible/tests/_loki_streams.py came from one k8s pod stream. a selector may legitimately carry
#      labels this set does not list. Widen the set against a live
#      stream if a genuine selector ever fails — do not delete the guard.


def test_the_alloy_config_is_actually_readable():
    """A path typo would make _alloy_relabel_targets() return an empty set, silently reducing
    the vocabulary to the transcribed floor and re-opening the gap this closes."""
    # `program` is the one label a `stage.labels` block sets rather than a relabel rule.
    assert _alloy_relabel_targets() >= {
        "container",
        "pod",
        "namespace",
        "machine",
        "program",
    }, (
        "the expected label names did not parse from the Alloy config — the path or "
        "the config shape changed, and the derived half of LOKI_STREAM_LABELS is now inert"
    )


def _logql_selector_names(cfg):
    """Every `Config` field that holds a LogQL stream selector, found by shape.

    Derived rather than listed. A hardcoded list would let a later selector, such as
    a later HA_BAN_SELECTOR, join unchecked. A selector this cannot see is a selector that can
    name a label Alloy does not emit and go permanently green, which is the exact failure
    the test exists for.

    Matched on the LEADING `{...}` only, deliberately: a selector may carry line filters
    after the closing brace (`{...} |~ "Banned IP"`), and requiring the string to END in `}`
    silently drops HA_BAN_SELECTOR -- narrowing the roster while looking like it widened it.
    """
    return sorted(
        f.name
        for f in fields(cfg)
        if isinstance(getattr(cfg, f.name), str)
        and getattr(cfg, f.name).startswith("{")
        and "}" in getattr(cfg, f.name)
    )


def _deployed_selector_values():
    """LogQL selector values that actually deploy, read from the rendered pod environment.

    `_logql_selector_names()` reads the `Config` fields. This reads every key the render
    produces instead, so a selector written into a new key is checked even before any `Config`
    field reads it. It reads the RENDER rather than the template source because the selectors
    are Jinja over `loki_streams` (#3740), and the source text of one is not a selector.
    """
    return {
        key: value
        for key, value in bridge_env().items()
        if value.startswith("{") and "}" in value
    }


def test_the_selector_roster_covers_the_known_selectors(cfg):
    """A shape-derived roster that matches nothing passes every assertion vacuously, and one
    that matches fewer than the known selectors is a silent narrowing."""
    names = set(_logql_selector_names(cfg))
    known = {
        "HA_BAN_SELECTOR",
        "LOG_ERROR_SELECTOR",
    }
    assert known <= names, (
        "the derived roster no longer covers known selectors, so they are unchecked: %s"
        % sorted(known - names)
    )


def test_loki_selectors_use_real_stream_labels(cfg):
    for name in _logql_selector_names(cfg):
        selector = getattr(cfg, name)
        unknown = _selector_labels(selector) - LOKI_STREAM_LABELS
        assert not unknown, (
            "%s selects on %s, which Alloy does not emit — the query matches no stream and "
            "the check goes permanently green: %s" % (name, sorted(unknown), selector)
        )


def test_the_deployed_selector_roster_covers_known_overrides():
    """A render that stopped producing the selectors would make `_deployed_selector_values()`
    return an empty dict, and every loop over it below would pass on nothing."""
    known = {
        "LOG_ERROR_SELECTOR",
    }
    deployed = set(_deployed_selector_values())
    assert known <= deployed, (
        "the deployed-selector roster no longer covers the known overrides, so they are "
        "unchecked: %s" % sorted(known - deployed)
    )


def test_deployed_loki_selectors_use_real_stream_labels():
    """Every rendered selector, including one no `Config` field reads yet, names real labels.

    This is a regression guard, not an active finding: the deployed selectors select on `job`
    and `machine`, which are real Alloy labels. It exists for the NEXT edit to a selector --
    HA_BAN_SELECTOR is the precedent (see this role's CLAUDE.md): an `app=` label matched no
    stream and read "no ip_ban events" permanently green.
    """
    for name, selector in _deployed_selector_values().items():
        unknown = _selector_labels(selector) - LOKI_STREAM_LABELS
        assert not unknown, (
            "%s deploys as %s, which selects on %s -- Alloy does not emit that label, so "
            "the query matches no stream and the check goes permanently green"
            % (name, selector, sorted(unknown))
        )


def test_log_error_inert_when_the_selector_matches_nothing():
    """Zero total volume must report INERT, never OK.

    The arm fails open, so a wrong selector produces no matches and reads exactly like a
    healthy estate. This is the HA_BAN_SELECTOR trap generalised: a fail-open check goes green
    on a typo. The total-volume count is the only thing separating "nothing is wrong" from
    "I asked the wrong question", and this test is what keeps it load-bearing.
    """
    ok, msg = checks.logs.log_error_verdict([], 0, 20, "1h")
    assert ok
    assert "INERT" in msg, (
        "a selector matching no lines must SAY so, not read as healthy"
    )


def test_log_error_quiet_estate_is_ok():
    ok, msg = checks.logs.log_error_verdict([], 5000, 20, "1h")
    assert ok
    assert "no log-error bursts" in msg


def test_log_error_names_the_offending_container():
    ok, msg = checks.logs.log_error_verdict(
        [({"container": "grafana"}, 91.0), ({"container": "sonarr"}, 3.0)],
        5000,
        20,
        "1h",
    )
    assert not ok
    assert "grafana (91)" in msg
    assert "sonarr" not in msg, "a container under the threshold is not an offender"


def test_log_error_orders_offenders_worst_first():
    _, msg = checks.logs.log_error_verdict(
        [({"container": "quiet"}, 21.0), ({"container": "loud"}, 900.0)], 5000, 20, "1h"
    )
    assert msg.index("loud") < msg.index("quiet")


def test_log_error_ignore_list_is_case_insensitive():
    ok, _ = checks.logs.log_error_verdict(
        [({"container": "Chatty"}, 900.0)], 5000, 20, "1h", ignore={"chatty"}
    )
    assert ok


def test_log_error_burst_wins_the_message_over_healthy_workloads(cfg):
    """A Ready-but-failing workload pages even though the verdict it folds into reads healthy.

    That combination IS the finding: readiness asks whether the port is open.
    """
    cfg = replace(cfg, LOG_ERROR_SELECTOR='{job=~"k8s|pi"}', LOG_ERROR_IGNORE="")
    src = FakeSources(
        loki_vector=lambda q: [({"container": "grafana"}, 91.0)],
        loki_count=lambda selector, window: 5000,
    )

    ok, msg = checks.logs.with_log_errors(cfg, src, True, "42 k8s workloads healthy")

    assert not ok
    assert msg.startswith("fatal log lines"), "the actionable arm leads"
    assert "42 k8s workloads healthy" in msg, (
        "the workload arm's text is kept, not dropped"
    )


def test_log_error_arm_fails_open_on_a_loki_outage(cfg):
    """A Loki outage must not turn the folded verdict into a page about Loki.

    This is why the row is gated by `startup_grace` alone: a Loki gate would suppress the whole
    check during a Loki outage.
    """
    cfg = replace(cfg, LOG_ERROR_SELECTOR='{job=~"k8s|pi"}')

    def boom(*a, **k):
        raise RuntimeError("loki query status=error")

    src = FakeSources(loki_vector=boom)

    ok, msg = checks.logs.with_log_errors(cfg, src, False, "2 workloads unavailable")

    assert not ok, "the workload verdict survives the arm being unavailable"
    assert "2 workloads unavailable" in msg
    assert "log-error arm unavailable" in msg, "the arm must say it could not evaluate"


def test_workload_log_errors_pages_on_a_burst(cfg):
    """The Workload Log Errors row is DOWN when one container logs more fatal lines than the max."""
    cfg = replace(cfg, LOG_ERROR_SELECTOR='{job=~"k8s|pi"}', LOG_ERROR_IGNORE="")
    src = FakeSources(
        loki_vector=lambda q: [({"container": "grafana"}, 91.0)],
        loki_count=lambda selector, window: 5000,
    )

    ok, msg = checks.logs.check_workload_log_errors(cfg, src)

    assert not ok
    assert msg.startswith("fatal log lines")


def test_workload_log_errors_stays_up_on_a_loki_error(cfg):
    """A Loki error is the loki-homelab ruler's page, so this row reports up and says it could not read."""
    cfg = replace(cfg, LOG_ERROR_SELECTOR='{job=~"k8s|pi"}')

    def boom(*a, **k):
        raise RuntimeError("loki query status=error")

    ok, msg = checks.logs.check_workload_log_errors(cfg, FakeSources(loki_vector=boom))

    assert ok
    assert "log-error arm unavailable" in msg


def _loki_scalar(val):
    """A Loki instant-query response for `sum(count_over_time(...))`. None -> empty result."""
    if val is None:
        return {"status": "success", "data": {"resultType": "vector", "result": []}}
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [{"metric": {}, "value": [1700000000, str(val)]}],
        },
    }


def _answering(body):
    """An opener in `urllib.request.urlopen`'s shape that answers every request with `body`."""
    return lambda req, timeout=None: io.BytesIO(json.dumps(body).encode())


def test_loki_count_parses_value(cfg):
    opener = _answering(_loki_scalar(42))
    assert bridge.net.loki_count(cfg, '{job="syslog"}', "10m", opener=opener) == 42.0


def test_loki_count_empty_result_is_none(cfg):
    opener = _answering(_loki_scalar(None))
    assert bridge.net.loki_count(cfg, '{job="syslog"}', "10m", opener=opener) is None


def test_loki_count_non_success_raises(cfg):
    opener = _answering({"status": "error"})
    with pytest.raises(RuntimeError):
        bridge.net.loki_count(cfg, '{job="syslog"}', "10m", opener=opener)
