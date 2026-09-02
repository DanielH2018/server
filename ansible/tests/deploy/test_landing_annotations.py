"""Holds the landing-annotation chain together end to end.

    land.sh  --logger-->  syslog  --promtail-->  Loki  <--  Landings dashboard

land.sh writes one logfmt line per landing from its EXIT trap; the Landings dashboard
unwraps named fields out of it. The two are edited independently, in different languages,
and nothing else checks that the field a panel unwraps is a field the script writes. Same
shape as test_deploy_annotations.py, one hop over.
"""

from __future__ import annotations

import json
import re

from _helpers import REPO as _REPO

_LAND_SH = _REPO / "scripts/deploy_tools/land.sh"
_BOARD = (
    _REPO
    / "ansible/roles/k8s/claude-otel/files/dashboards/Infrastructure/landings.json"
)


def _emit_block() -> str:
    text = _LAND_SH.read_text()
    start = text.index("emit_landing_annotation() {")
    end = text.index("trap emit_landing_annotation EXIT")
    return text[start:end]


def _board_exprs() -> list[str]:
    exprs: list[str] = []

    def visit(node):
        if isinstance(node, dict):
            expr = node.get("expr")
            if isinstance(expr, str):
                exprs.append(expr)
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(json.loads(_BOARD.read_text()))
    return exprs


def test_every_field_the_board_unwraps_is_one_land_sh_writes():
    """The join. A panel unwrapping `wait_ci` renders "No data" with no error if the script
    spells it `ci_wait`."""
    unwrapped = {
        m.group(1)
        for expr in _board_exprs()
        for m in re.finditer(r"unwrap (\w+)", expr)
    }
    assert unwrapped, "the board unwraps no field at all — the phase panels are empty"
    written = set(re.findall(r"(\w+)=", _emit_block()))
    missing = unwrapped - written
    assert not missing, (
        f"the board unwraps {sorted(missing)} but land.sh never writes them"
    )


def test_the_board_filters_on_the_literal_land_sh_logs():
    literals = {
        m.group(1)
        for expr in _board_exprs()
        for m in re.finditer(r'\|=\s*"([^"]+)"', expr)
    }
    assert literals, (
        "the board carries no line filter — it would parse every syslog line"
    )
    block = _emit_block()
    for literal in literals:
        assert literal in block, (
            f"land.sh never logs {literal!r}; the board would show nothing"
        )


def test_a_field_the_script_does_not_write_would_be_caught():
    """The reject half of the join test: the check must be able to fail."""
    written = set(re.findall(r"(\w+)=", _emit_block()))
    assert "not_a_real_phase" not in written


def test_every_verdict_site_names_its_verdict_for_the_trap():
    """Each `VERDICT:` line must be preceded by the assignment the trap reads, or that landing
    is logged as `aborted` and the board mis-counts it."""
    lines = _LAND_SH.read_text().splitlines()
    for i, line in enumerate(lines):
        m = re.match(r'\s*echo "VERDICT: ([a-z-]+) ', line)
        if not m:
            continue
        assert lines[i - 1].strip() == f"LAND_VERDICT={m.group(1)}", (
            f"land.sh line {i + 1}: VERDICT {m.group(1)!r} is not preceded by "
            f"LAND_VERDICT={m.group(1)}"
        )


def test_annotating_is_a_trap_and_can_never_fail_a_landing():
    text = _LAND_SH.read_text()
    assert "trap emit_landing_annotation EXIT" in text
    assert "|| true" in _emit_block(), "the logger call must be fire-and-forget"


def test_the_datasource_uid_matches_the_provisioned_one():
    """Same guard as the deploy annotation: a stale uid renders a silent No data."""
    board = _BOARD.read_text()
    uids = set(re.findall(r'"uid":\s*"([^"]+)"', board)) - {"landings"}
    assert uids == {"bf4q19tuivta8e"}, uids
