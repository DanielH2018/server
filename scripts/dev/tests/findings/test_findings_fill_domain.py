"""`fill_domain.py` copies each `domain/` label into the Domain issue field, once per issue.

Run: uv run pytest scripts/dev/tests/findings/test_findings_fill_domain.py
"""

from _findings_fakes import Fakes, build_tools, make_issue

from dev import fill_domain


def _field_writes(calls) -> list[str]:
    return [
        c[3]
        for c in calls.gh_json
        if c[:2] == ["api", "graphql"] and "setIssueFieldValue" in c[3]
    ]


def _run(issues, *argv):
    tools, calls = build_tools(Fakes(issues=issues))
    pauses: list[float] = []
    code = fill_domain.main(list(argv), tools, pauses.append)
    return code, calls, pauses


def test_fill_domain_writes_the_label_into_the_field_and_skips_a_filled_issue():
    labelled = make_issue(7, labels=["claude", "domain/network"])
    filled = {
        **make_issue(8, labels=["claude", "domain/cicd"]),
        "fields": {"Domain": "cicd"},
    }
    unlabelled = make_issue(9, labels=["claude"])
    code, calls, _ = _run([labelled, filled, unlabelled])
    assert code == 0
    (write,) = _field_writes(calls)
    assert '"IF_dom"' in write and '"o_network"' in write and '"I_7"' in write


def test_fill_domain_pauses_between_mutations_but_not_before_the_first():
    issues = [make_issue(n, labels=["claude", "domain/docs"]) for n in (1, 2, 3)]
    _, calls, pauses = _run(issues)
    assert len(_field_writes(calls)) == 3
    assert len(pauses) == 2


def test_fill_domain_dry_run_writes_nothing(capsys):
    _, calls, pauses = _run(
        [make_issue(7, labels=["claude", "domain/network"])], "--dry-run"
    )
    assert not _field_writes(calls) and not pauses
    assert "Domain=network" in capsys.readouterr().out
