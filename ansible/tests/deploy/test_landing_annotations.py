"""Holds the landing-annotation chain together end to end.

    land.py  --logger-->  syslog  --alloy-->  Loki  <--  Landings dashboard

land_lib/ledger.py writes one logfmt line per landing; the Landings dashboard unwraps named
fields out of it. The two are edited independently, and nothing else checks that the field
a panel unwraps is a field the script writes. Same shape as test_deploy_annotations.py.
"""

from __future__ import annotations

import json
import re

from _helpers import REPO as _REPO
from deploy_tools.land_lib.ledger import Ledger, annotation_line

_BOARD = (
    _REPO
    / "ansible/roles/k8s/claude-otel/files/dashboards/Infrastructure/landings.json"
)


def _sample_line() -> str:
    return annotation_line(
        Ledger(pr="1", t_start=0.0, merge_sha="0123456789abcdef", verdict="settled"),
        0,
        12.0,
    )


def _board_exprs() -> list[str]:
    exprs: list[str] = []

    def visit(node):
        if isinstance(node, dict):
            if isinstance(node.get("expr"), str):
                exprs.append(node["expr"])
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(json.loads(_BOARD.read_text()))
    return exprs


def test_every_field_the_board_unwraps_is_one_the_ledger_writes():
    unwrapped = {
        m.group(1) for e in _board_exprs() for m in re.finditer(r"unwrap (\w+)", e)
    }
    assert unwrapped, "the board unwraps no field at all"
    written = set(re.findall(r"(\w+)=", _sample_line()))
    assert not (unwrapped - written), unwrapped - written


def test_the_board_filters_on_a_literal_the_ledger_writes():
    literals = {
        m.group(1) for e in _board_exprs() for m in re.finditer(r'\|=\s*"([^"]+)"', e)
    }
    assert literals
    for literal in literals:
        assert literal in _sample_line(), literal


def test_a_field_the_ledger_does_not_write_would_be_caught():
    assert "not_a_real_phase" not in set(re.findall(r"(\w+)=", _sample_line()))


def test_an_unreached_stamp_leaves_its_field_empty():
    line = annotation_line(Ledger(pr="1", t_start=0.0), 75, 5.0)
    assert " wait_merge= " in line and "verdict=aborted" in line


def test_the_datasource_uid_matches_the_provisioned_one():
    uids = set(re.findall(r'"uid":\s*"([^"]+)"', _BOARD.read_text())) - {"landings"}
    assert uids == {"bf4q19tuivta8e"}, uids


def test_a_deploy_failed_line_names_its_cause():
    """Issue #1031: `deploy-failed` alone cannot tell "nothing deployed" from "changes are
    live", so the cause rides on the same line. Every other verdict leaves it empty, like an
    unreached stamp."""
    line = annotation_line(
        Ledger(pr="1", t_start=0.0, verdict="deploy-failed", cause="tag-miss"), 1, 3.0
    )
    assert "verdict=deploy-failed cause=tag-miss " in line
    assert " cause= " in annotation_line(Ledger(pr="1", t_start=0.0), 75, 5.0)
