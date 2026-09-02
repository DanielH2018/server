"""Tests for scripts/dev/register_audit.py.

Builds a small synthetic register + fake repo tree in tmp_path rather than
depending on the real memory file, which is edited every review run. See
register_audit.py's module docstring for why a bare citation resolving is not
itself closure evidence.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from register_audit import (  # noqa: E402
    STATUS_CLOSED,
    STATUS_OPEN,
    STATUS_UNRESOLVED,
    audit,
    find_section,
    parse_table_rows,
)

REGISTER_HEADER = """\
## Deliberate trade-offs

Some unrelated prose that must not be parsed as the register table.

| Item | First seen | Runs carried |
|---|---|---|
| decoy row from a different table | 2026-01-01 | 9 |

## Open and recurring — report as recurrence, not discovery

Confirmed real, nobody working them.

| Item | First seen | Runs carried |
|---|---|---|
{rows}

## Closed / retired

This section must not be parsed as part of the open table.

| Item | First seen | Runs carried |
|---|---|---|
| a closed row that must never appear | 2020-01-01 | 1 |
"""


def make_register(tmp_path: Path, *rows: str) -> Path:
    text = REGISTER_HEADER.format(rows="\n".join(rows))
    path = tmp_path / "register.md"
    path.write_text(text)
    return path


def make_repo(tmp_path: Path, *relpaths: str) -> Path:
    repo = tmp_path / "repo"
    for rel in relpaths:
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("stub")
    return repo


def test_find_section_is_heading_anchored():
    text = "## A\nignored\n## Open and recurring — report as recurrence, not discovery\nbody\n## C\nignored"
    assert find_section(text, "Open and recurring").strip() == "body"


def test_find_section_missing_heading_raises():
    import pytest

    with pytest.raises(LookupError):
        find_section("## Something else\nbody", "Open and recurring")


def test_parse_table_rows_stops_at_next_section(tmp_path):
    path = make_register(
        tmp_path,
        "| plain open row with no evidence | 2026-01-01 | 1 |",
    )
    text = path.read_text()
    section = find_section(text, "Open and recurring")
    rows = parse_table_rows(section)
    assert len(rows) == 1
    assert rows[0].item == "plain open row with no evidence"
    assert rows[0].first_seen == "2026-01-01"
    assert rows[0].runs_carried == "1"
    # the decoy row above the heading, and the closed row below it, must not leak in
    assert all("decoy" not in r.item and "never appear" not in r.item for r in rows)


def test_an_escaped_pipe_in_the_prose_does_not_shift_the_columns(tmp_path):
    """A Markdown cell boundary is an UNESCAPED pipe.

    The live push-token row carries a backticked `grep -E 'a\\|b\\|c'` alternation. Splitting
    on every `|` walked the cell boundaries into the middle of the sentence and reported
    `runs=longhorn_backup\\` — a row parsed into the wrong columns, which is worse than one
    that fails to parse, because it still prints a plausible-looking answer.
    """
    path = make_register(
        tmp_path,
        r"| token row citing `grep -E '(a\|b\|c)_push_token'` here | 2026-02-02 | 3 |",
    )
    rows = parse_table_rows(find_section(path.read_text(), "Open and recurring"))
    assert len(rows) == 1
    assert rows[0].first_seen == "2026-02-02"
    assert rows[0].runs_carried == "3"
    assert "push_token" in rows[0].item


def test_unresolved_when_no_reference_extractable(tmp_path):
    register = make_register(
        tmp_path,
        "| 'State coupled outside the volume' has no backtick or bold evidence at all | 2026-08-20 | 2 |",
    )
    repo = make_repo(tmp_path, "scripts/diagnostics/probe.py")
    results = audit(register, repo)
    assert len(results) == 1
    assert results[0].status == STATUS_UNRESOLVED
    assert results[0].reference is None


def test_open_when_bare_citation_exists_but_is_only_the_anchor(tmp_path):
    """A cited `path:line` that merely resolves is context, not closure evidence --
    it almost always resolves, since it was correct when the row was written."""
    register = make_register(
        tmp_path,
        "| Alert history is blind to host-cron pushers (`probe.py:1261`) | 2026-08-17 | 3 |",
    )
    repo = make_repo(tmp_path, "scripts/diagnostics/probe.py")
    results = audit(register, repo)
    assert results[0].status == STATUS_OPEN
    assert results[0].reference is not None
    assert results[0].reference.tier == "C"
    assert results[0].reference.resolved is not None


def test_open_when_cited_path_does_not_resolve(tmp_path):
    register = make_register(
        tmp_path,
        "| Something cites a file that is gone (`scripts/gone.py:12`) | 2026-08-17 | 3 |",
    )
    repo = make_repo(tmp_path, "scripts/diagnostics/probe.py")
    results = audit(register, repo)
    assert results[0].status == STATUS_OPEN
    assert results[0].reference.resolved is None


def test_likely_closed_via_fixture_heuristic(tmp_path):
    """The check_b2_storage case: no path is cited at all, but this repo's
    convention gives a `check_X` function a `test_X.py` fixture."""
    register = make_register(
        tmp_path,
        "| `check_b2_storage`'s live call shape is unverified. "
        "**Give it a recorded-response fixture** | 2026-08-15b | 4 |",
    )
    repo = make_repo(
        tmp_path,
        "ansible/roles/k8s/monitor-bridge/files/check.py",
        "ansible/roles/k8s/monitor-bridge/files/test_b2_storage.py",
    )
    results = audit(register, repo)
    assert results[0].status == STATUS_CLOSED
    assert results[0].reference.tier == "A"
    assert results[0].reference.resolved.name == "test_b2_storage.py"


def test_open_via_fixture_heuristic_when_no_fixture_exists(tmp_path):
    register = make_register(
        tmp_path,
        "| `check_backup_health`'s live call shape is unverified. "
        "**Give it a recorded-response fixture** | 2026-08-15b | 4 |",
    )
    repo = make_repo(tmp_path, "ansible/roles/k8s/monitor-bridge/files/check.py")
    results = audit(register, repo)
    assert results[0].status == STATUS_OPEN


def test_likely_closed_via_keyword_search(tmp_path):
    """The Longhorn Grafana dashboard case: the citation (`check.py:2738`) is the
    pre-existing check arm, not the missing dashboard; closure evidence is a
    *different* path matching the row's own vocabulary."""
    register = make_register(
        tmp_path,
        "| The Longhorn **Grafana dashboard** half (the degraded-volume check arm "
        "exists at `check.py:2738`) | 2026-08-17 | 3 |",
    )
    repo = make_repo(
        tmp_path,
        "ansible/roles/k8s/monitor-bridge/files/check.py",
        "ansible/roles/k8s/claude-otel/files/dashboards/Infrastructure/longhorn.json",
    )
    results = audit(register, repo)
    assert results[0].status == STATUS_CLOSED
    assert results[0].reference.tier == "B"
    assert results[0].reference.resolved.name == "longhorn.json"


def test_open_via_keyword_search_when_no_matching_path_exists(tmp_path):
    register = make_register(
        tmp_path,
        "| The Longhorn **Grafana dashboard** half (the degraded-volume check arm "
        "exists at `check.py:2738`) | 2026-08-17 | 3 |",
    )
    repo = make_repo(tmp_path, "ansible/roles/k8s/monitor-bridge/files/check.py")
    results = audit(register, repo)
    assert results[0].status == STATUS_OPEN


def test_keyword_search_requires_two_cooccurring_keywords(tmp_path):
    """One matching keyword out of several extracted must not be enough."""
    register = make_register(
        tmp_path,
        "| An **ipBlock host-originated** requirement, still not delivered | 2026-08-17 | 3 |",
    )
    # "host" is in this path but "ipblock"/"originated" are not -- only 1 of the
    # 3 extracted keywords co-occurs, below the 2-keyword threshold.
    repo = make_repo(
        tmp_path,
        "ansible/roles/k8s/netpol-baseline/templates/networkpolicy-host.yaml.j2",
    )
    results = audit(register, repo)
    assert results[0].status == STATUS_OPEN
    assert results[0].reference is None


def test_keyword_search_skips_unanswered_ask_boilerplate(tmp_path):
    """A row whose bold text is this register's standing "asked and not answered"
    marker is a status note, not a description of missing evidence -- it must not
    let an incidental capitalized word (e.g. "Grafana" in ordinary prose) trigger
    a false LIKELY-CLOSED the way a single-keyword match did for the real
    Alert-history row (it matched grafana-secret.yaml.j2 for an unrelated gap)."""
    register = make_register(
        tmp_path,
        "| The Grafana board is missing a thing — **asked for in the network brief "
        "and NOT answered; not re-verified** | 2026-08-17 | 3 |",
    )
    repo = make_repo(
        tmp_path, "ansible/roles/k8s/claude-otel/templates/grafana-secret.yaml.j2"
    )
    results = audit(register, repo)
    assert results[0].status != STATUS_CLOSED


def test_multiple_rows_all_reported(tmp_path):
    """Never silently drop a row, including an UNRESOLVED one."""
    register = make_register(
        tmp_path,
        "| `check_b2_storage` fixture ask. **Give it a fixture** | 2026-08-15b | 4 |",
        "| no evidence at all here | 2026-08-20 | 2 |",
        "| cites `probe.py:1261` only | 2026-08-17 | 3 |",
    )
    repo = make_repo(
        tmp_path,
        "ansible/roles/k8s/monitor-bridge/files/test_b2_storage.py",
        "scripts/diagnostics/probe.py",
    )
    results = audit(register, repo)
    assert len(results) == 3
    statuses = {r.status for r in results}
    assert statuses == {STATUS_CLOSED, STATUS_UNRESOLVED, STATUS_OPEN}


def test_main_exits_zero_always(tmp_path, capsys):
    register = make_register(tmp_path, "| no evidence here | 2026-08-20 | 2 |")
    repo = make_repo(tmp_path, "scripts/diagnostics/probe.py")
    from register_audit import main

    rc = main(["--register", str(register), "--repo", str(repo)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "UNRESOLVED" in out
    assert "register audit:" in out
