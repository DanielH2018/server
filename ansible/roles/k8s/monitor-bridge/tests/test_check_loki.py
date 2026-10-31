"""Loki: the selector roster and the count helper.

A LogQL selector naming a label Alloy does not ship matches no stream and reports "no events"
forever. HA_BAN_SELECTOR shipped that way with app="home-assistant". A fail-open arm cannot tell
"nothing to report" from "wrong question", so the selector labels are checked against the
observed stream vocabulary rather than against the check's own verdict.
"""

import io
import json

from dataclasses import fields

import pytest

import bridge.config
import bridge.net

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
#   1. It reads each selector off the `cfg` fixture's string fields. HA_BAN_SELECTOR is a default in
#      bridge/config_service.py, not a rendered key, so the field is what is checked. A selector built
#      at runtime from other values is not.
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


def test_the_selector_roster_covers_the_known_selectors(cfg):
    """A shape-derived roster that matches nothing passes every assertion vacuously, and one
    that matches fewer than the known selectors is a silent narrowing."""
    names = set(_logql_selector_names(cfg))
    known = {
        "HA_BAN_SELECTOR",
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
