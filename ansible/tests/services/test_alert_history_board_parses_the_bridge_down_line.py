#!/usr/bin/env python3
"""The Alert History board's LogQL reads the DOWN line monitor-bridge WRITES today (#1793).

The board reconstructs per-check DOWN counts from `{container="monitor-bridge"}` with a parser
stage, and that stage encoded the bracketed stamp `bridge.common.log` printed until bec8990a
(2026-09-04). Grafana's `pattern` parser matched nothing from that date on, so both per-check
panels showed one unlabelled series — the same reader-side break #1782 fixed in
`probe.py alerts`. `test_monitor_bridge_down_line_shape.py` guards that reader; this guards the
board, by pulling the regexp out of the committed JSON and feeding it the real emitter's output.

Python `re` stands in for Loki's RE2: the expression uses only a `(?P<name>)` group, a class,
`?`, `+` and `\\S`, which the two engines read alike.

Run: uv run pytest ansible/tests/services/test_alert_history_board_parses_the_bridge_down_line.py
"""

import json
import re
from _helpers import ROLES

import bridge.common
import pytest

BOARD = ROLES / "k8s/claude-otel/files/dashboards/Infrastructure/alert-history.json"
# The panels that carry the monitor-bridge parser. A rename here must land in the census below,
# or the test would pass over an empty set.
PARSING_PANELS = frozenset(
    {"DOWN cycles by check (range total)", "DOWN cycles over time, by check"}
)
_REGEXP_STAGE = re.compile(r"\| regexp `([^`]+)`")


def _monitor_bridge_parsers() -> dict[str, str]:
    """{panel title: regexp} for every monitor-bridge expression that carries a regexp stage."""
    doc = json.loads(BOARD.read_text())
    found = {}
    for panel in doc["panels"]:
        for target in panel.get("targets", []):
            expr = target.get("expr", "")
            if 'container="monitor-bridge"' not in expr:
                continue
            m = _REGEXP_STAGE.search(expr)
            if m:
                found[panel["title"]] = m.group(1)
    return found


def _logged(capsys, *args):
    bridge.common.log(*args)
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 1, f"expected exactly one log line, got {out!r}"
    return out[0]


def test_both_per_check_panels_parse_with_a_regexp_stage():
    """Non-vacuity: the census names the panels, so a retitled or dropped one fails here."""
    assert set(_monitor_bridge_parsers()) == PARSING_PANELS


@pytest.mark.parametrize("title", sorted(PARSING_PANELS))
def test_the_down_line_the_bridge_logs_today_yields_the_check_name(capsys, title):
    """ACCEPT: the unstamped line the bridge prints since 2026-09-04 labels by check."""
    line = _logged(
        capsys, "DOWN", "traefik_latency", "-", "1 slow service(s) (3 cycles)"
    )
    m = re.match(_monitor_bridge_parsers()[title], line)

    assert m is not None and m["name"] == "traefik_latency"


@pytest.mark.parametrize("title", sorted(PARSING_PANELS))
def test_the_stamped_line_loki_still_retains_yields_the_check_name(title):
    """ACCEPT: a pre-bec8990a line stays readable while Loki's 31-day retention holds it."""
    line = "[2026-09-04T09:31:04] DOWN pi_pressure - pi_pressure check error: timed out (5 cycles)"
    m = re.match(_monitor_bridge_parsers()[title], line)

    assert m is not None and m["name"] == "pi_pressure"


@pytest.mark.parametrize("title", sorted(PARSING_PANELS))
def test_an_ok_line_that_mentions_down_is_not_a_check(capsys, title):
    """REJECT: `OK swallowed_verdicts - no swallowed DOWN verdicts` contains DOWN and is not one."""
    line = _logged(
        capsys, "OK  ", "swallowed_verdicts", "-", "no swallowed DOWN verdicts in 3h"
    )

    assert re.match(_monitor_bridge_parsers()[title], line) is None


def test_every_monitor_bridge_expression_filters_on_a_line_anchored_down():
    """The line filter, not the parser, is what keeps an OK line out of the count: LogQL passes
    a line a regexp stage does not match straight through, unlabelled, and `sum by (name)`
    counts it under `{}` — 21 such lines over 2d, measured 2026-09-17."""
    doc = json.loads(BOARD.read_text())
    exprs = [
        t["expr"]
        for p in doc["panels"]
        for t in p.get("targets", [])
        if 'container="monitor-bridge"' in t.get("expr", "")
    ]

    assert len(exprs) == 3, exprs
    assert all("|~ `^(\\[[^\\]]+\\] )?DOWN `" in e for e in exprs), exprs
    assert not any('|= "DOWN"' in e for e in exprs), exprs
