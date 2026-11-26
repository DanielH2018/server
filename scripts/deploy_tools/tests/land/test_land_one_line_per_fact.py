"""Each landing fact prints as one line, once.

The 2026-11-09 audit of 162 landing logs found facts printed twice in a row, repeated on
every merge attempt, or spread over several lines of raw `gh` text.

Run: uv run pytest scripts/deploy_tools/tests/land/test_land_one_line_per_fact.py
"""

import subprocess
from dataclasses import replace

import pytest

from _land_fakes import Fakes
from deploy_tools.land_lib import merge
from deploy_tools.land_lib.outcome import Outcome

OLD = "a" * 40
# What `gh api -X PUT …/merge` writes to stderr when a required check has not finished.
RULE_VIOLATION = (
    "gh: Repository rule violations found\n\n"
    'Required status check "prek" is in progress.\n\n'
    " (HTTP 405)"
)


def _landing_refused_once(landing, stderr: str):
    """A green head whose first direct merge GitHub refuses with `stderr`."""
    polls = [{"state": "OPEN", "mergeable": "MERGEABLE", "headRefOid": OLD}] * 2 + [
        {"state": "MERGED", "mergeable": "MERGEABLE", "headRefOid": OLD}
    ]
    ln, _ = landing(
        Fakes(
            gh_views={"state,mergeable,headRefOid": polls},
            await_ci=[(0, "CI green")],
        )
    )
    ln.direct_merge_subject = "Document the lander"
    merges = []

    def gh(*args, **kwargs):
        if args[:3] == ("api", "-X", "PUT") and args[3].endswith("/merge"):
            merges.append(args)
            if len(merges) == 1:
                raise subprocess.CalledProcessError(1, args, stderr=stderr)
            return subprocess.CompletedProcess(args, 0, "", "")
        return ln_gh(*args, **kwargs)

    ln_gh = ln.tools.gh
    ln.tools = replace(ln.tools, gh=gh)
    return ln, merges


def test_a_rule_violation_refusal_prints_as_one_line_naming_the_check(landing, capsys):
    ln, merges = _landing_refused_once(landing, RULE_VIOLATION)
    merge.await_merge(ln)
    assert len(merges) == 2
    out = capsys.readouterr().out
    naming = [line for line in out.splitlines() if "prek" in line]
    assert len(naming) == 1 and "refused" in naming[0], out
    assert "HTTP 405" not in out and "Repository rule violations" not in out, out


def test_a_refusal_states_its_sentence_and_pr_number_once(landing, capsys):
    """`land: <msg>` on stderr and the VERDICT on stdout repeated each other."""
    ln, _ = landing()
    with pytest.raises(Outcome) as exc:
        ln.die("the tick refused the range", 1)
    exc.value.emit()
    cap = capsys.readouterr()
    both = cap.out + cap.err
    assert both.count("the tick refused the range") == 1, both
    assert both.count("PR #999") <= 1, both
    assert "VERDICT: refused" in cap.out, both


def test_arming_a_direct_merge_prints_one_line(land_run):
    """The phase header said "arming", then the arm step said "not arming auto-merge"."""
    fakes = Fakes(
        gh_views={
            "state,title,body,reviewDecision": {
                "state": "OPEN",
                "title": "Document the lander",
                "body": "",
            },
            "state,mergeable,headRefOid": [
                {"state": "MERGED", "mergeable": "MERGEABLE", "headRefOid": OLD}
            ],
        }
    )
    _, out, _, _, _ = land_run(["--arm-merge", "--await-merge"], fakes)
    arming = [line for line in out.splitlines() if "arming" in line.lower()]
    assert len(arming) == 1, out
