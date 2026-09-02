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


_VERDICT_HEADER = re.compile(
    r"# Verdicts printed on stdout: (.+?)\.\n# The last four", re.S
)
_DIE_WITH_VERDICT = re.compile(r'\bdie "[^"]*" (\d+) ([a-z-]+)\s*(?:;;)?$')
_DIE_75_BARE = re.compile(r'\bdie "[^"]*" 75\s*(?:;;)?$')


def _documented_verdicts() -> set[str]:
    m = _VERDICT_HEADER.search(_LAND_SH.read_text())
    assert m, "land.sh's header no longer lists its verdicts"
    return {v.strip() for v in m.group(1).replace("\n#", " ").split("|")}


def test_every_die_verdict_is_a_documented_one():
    """A `die` verdict the header does not list is a name the board and the skill never learn."""
    named = {
        m.group(2)
        for line in _LAND_SH.read_text().splitlines()
        if (m := _DIE_WITH_VERDICT.search(line))
    }
    undocumented = named - _documented_verdicts()
    assert not undocumented, (
        f"die() names verdicts the header does not list: {sorted(undocumented)}"
    )


def test_every_exit_75_die_names_a_verdict():
    """Every `die ... 75` — a budget elapsed, or the lock stayed busy — carries a verdict.

    Before 2026-09-02 all of them reached the Landings board as `aborted`: two merge-budget
    timeouts and two CI-budget timeouts read as one bucket, and were taken for lock contention.
    """
    bare = [
        line.strip()
        for line in _LAND_SH.read_text().splitlines()
        if _DIE_75_BARE.search(line)
    ]
    assert not bare, f"exit-75 die sites with no verdict: {bare}"


def test_a_bare_exit_75_die_would_be_caught():
    assert _DIE_75_BARE.search('  75) die "lock stayed busy" 75 ;;')
    assert not _DIE_75_BARE.search('  75) die "lock stayed busy" 75 lock-busy ;;')


def _retry_loop(text: str, call: str) -> str:
    start = text.index(call)
    return text[start : text.index("\ndone\n", start)]


def test_lock_contention_is_recorded_in_both_retry_loops():
    """The tick loop and the deploy loop each retry on contention; each must book the wait."""
    text = _LAND_SH.read_text()
    tick_loop = _retry_loop(text, "gitops_tick.sh\n  tick_rc=$?")
    deploy_loop = _retry_loop(text, 'deploy.sh --tags "$TAGS"\n  deploy_rc=$?')
    assert "note_lock_contention" in tick_loop, (
        "the tick retry loop does not record its lock wait"
    )
    assert "note_lock_contention" in deploy_loop, (
        "the deploy retry loop does not record its lock wait"
    )


def test_nothing_to_deploy_is_decided_before_the_ci_wait():
    """A PR that reaches no service, plane or self-applied role exits before `await_ci.py`.

    Sixteen of the 45 landings before 2026-09-02 waited a median seven minutes of CI to
    learn there was nothing to deploy. The deployer's own tick fast-forwards those.
    """
    text = _LAND_SH.read_text()
    shortcut = text.index("LAND_VERDICT=nothing-to-deploy")
    ci_wait = text.index('await_ci.py "$MERGE_SHA"')
    assert shortcut < ci_wait, (
        "the nothing-to-deploy short-circuit sits after the CI wait"
    )


def test_the_diff_fallback_is_not_short_circuited():
    """A truncated file list derives from `$SINCE...HEAD`, and HEAD is the primary checkout
    the tick has not fast-forwarded yet — so that path must still go through the tick."""
    text = _LAND_SH.read_text()
    shortcut = text.index("LAND_VERDICT=nothing-to-deploy")
    guard = text[text.rindex("\nif ", 0, shortcut) : shortcut]
    assert '[ -z "$NEEDS_DIFF" ]' in guard, (
        "the short-circuit ignores the truncated-file-list fallback"
    )
