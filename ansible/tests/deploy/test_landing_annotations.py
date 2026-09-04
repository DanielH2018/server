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


def _fields_the_board_unwraps_but_the_ledger_omits(exprs: list[str]) -> set[str]:
    """The unwrapped names in `exprs` that no `key=` in a sample annotation supplies."""
    unwrapped = {m.group(1) for e in exprs for m in re.finditer(r"unwrap (\w+)", e)}
    assert unwrapped, "the board unwraps no field at all"
    return unwrapped - set(re.findall(r"(\w+)=", _sample_line()))


def test_every_field_the_board_unwraps_is_one_the_ledger_writes():
    missing = _fields_the_board_unwraps_but_the_ledger_omits(_board_exprs())
    assert not missing, missing


def _labels_the_board_groups_by_but_the_ledger_omits(exprs: list[str]) -> set[str]:
    """The `sum by (x)` labels in `exprs` that no `key=` in a sample annotation supplies."""
    grouped = {
        name.strip()
        for e in exprs
        for m in re.finditer(r"by \(([^)]*)\)", e)
        for name in m.group(1).split(",")
        if name.strip()
    }
    assert grouped, "the board groups by no label at all"
    return grouped - set(re.findall(r"(\w+)=", _sample_line()))


def test_every_label_the_board_groups_by_is_one_the_ledger_writes():
    missing = _labels_the_board_groups_by_but_the_ledger_omits(_board_exprs())
    assert not missing, missing


def test_a_label_the_ledger_does_not_write_would_be_caught():
    planted = ['sum by (not_a_real_label) (count_over_time({job="syslog"} [$__range]))']
    assert _labels_the_board_groups_by_but_the_ledger_omits(
        [*_board_exprs(), *planted]
    ) == {"not_a_real_label"}


def test_the_board_breaks_deploy_failed_down_by_cause():
    """The reader half of issue #1031.

    Writing `cause` into the annotation splits nothing on its own: the board reported one
    `deploy-failed` bucket for six situations, and the field it needed was there for two
    days before a panel read it. This asserts the panel exists, not merely that the field
    can be parsed.
    """
    exprs = _board_exprs()
    assert any("by (cause)" in e and 'verdict="deploy-failed"' in e for e in exprs), (
        exprs
    )
    assert any("by (verdict)" in e for e in exprs), exprs


def test_the_board_filters_on_a_literal_the_ledger_writes():
    literals = {
        m.group(1) for e in _board_exprs() for m in re.finditer(r'\|=\s*"([^"]+)"', e)
    }
    assert literals
    for literal in literals:
        assert literal in _sample_line(), literal


def test_a_field_the_ledger_does_not_write_would_be_caught():
    """The rejecting half, driven through the same comparison the real test uses.

    Asserting only that the sample line lacks `not_a_real_phase` proved nothing about the
    comparison -- it would pass with the subtraction deleted. A planted expr must be
    reported as missing.
    """
    planted = ['{job="syslog"} | logfmt | unwrap not_a_real_phase [$__interval]']
    assert _fields_the_board_unwraps_but_the_ledger_omits(
        [*_board_exprs(), *planted]
    ) == {"not_a_real_phase"}


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
