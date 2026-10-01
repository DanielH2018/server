"""`open` dedups by asking gh's search index for one fingerprint, not by reading the register.

The register only grows, and a whole-register read grows with it: 995 issues are 4.3 MB and
58.4s. Past `lib.gh.gh`'s 60s default EVERY findings.py subcommand died, `open` included, so a
session told to file what it did not fix had no filer at all. The search is 0.6s and does not
grow.

Two properties hold the fix up, and each has a test here. The QUERY is the fix, so one test
pins the argv shape and that no whole-register read happens beside it. `find_by_fingerprint`
is the safety net, so the other two pin that a hit gh returns for a fingerprint quoted
somewhere other than the body trailer dedups nothing.

Run: uv run pytest scripts/dev/tests/test_findings_open_dedup_search.py
"""

from _findings_fakes import Fakes
from dev import findings
from dev.findings_lib.gh_calls import FINGERPRINT_SEARCH_LIMIT, ISSUE_LIST_CAP
from dev.findings_lib.issue_model import fingerprint

_FP = fingerprint("T", "a.py")


def _open_argv(body):
    return [
        "open",
        "--title",
        "T",
        "--body-file",
        str(body),
        "--severity",
        "low",
        "--kind",
        "gap",
        "--file",
        "a.py",
    ]


def _issue_lists(calls):
    return [argv for argv in calls.gh_json if argv[:2] == ["issue", "list"]]


def test_open_searches_for_the_fingerprint_and_never_fetches_the_register(
    tmp_path, make_tools
):
    body = tmp_path / "b.md"
    body.write_text("B")
    tools, calls = make_tools(Fakes())
    assert findings.main(_open_argv(body), tools) == 0

    (argv,) = _issue_lists(calls)
    assert argv[argv.index("--search") + 1] == _FP
    assert argv[argv.index("--limit") + 1] == str(FINGERPRINT_SEARCH_LIMIT)
    # `--state all` stays: `plan_open` refuses to reopen a refuted or accepted finding, and
    # both of those are closed.
    assert argv[argv.index("--state") + 1] == "all"
    assert str(ISSUE_LIST_CAP) not in argv, "open still reads the whole register"


def test_a_fingerprint_gh_matched_in_a_comment_dedups_nothing(
    tmp_path, issue, make_tools
):
    """Search matches comments as well as bodies; only the body trailer owns a fingerprint.

    Without `find_by_fingerprint` filtering the hits, this quote would touch #3 and the real
    finding would never be filed.
    """
    body = tmp_path / "b.md"
    body.write_text("B")
    quoting = issue(3, comments=(f"same root cause as `{_FP}`",))
    tools, calls = make_tools(Fakes(issues=[quoting]))
    assert findings.main(_open_argv(body), tools) == 0
    assert [argv[:2] for argv in calls.gh] == [["issue", "create"]]


def test_a_full_page_of_hits_owning_nothing_warns_before_filing(
    tmp_path, capsys, issue, make_tools
):
    """The one way the search returns a wrong None: the owner pushed off the page.

    A silent create there is a duplicate nobody sees, so the warning is what makes the
    page limit safe to have.
    """
    body = tmp_path / "b.md"
    body.write_text("B")
    hits = [issue(n) for n in range(10, 10 + FINGERPRINT_SEARCH_LIMIT)]
    tools, calls = make_tools(Fakes(issues=hits))
    assert findings.main(_open_argv(body), tools) == 0
    assert f"{FINGERPRINT_SEARCH_LIMIT} search hits for fingerprint {_FP}" in (
        capsys.readouterr().err
    )
    assert [argv[:2] for argv in calls.gh] == [["issue", "create"]]


def test_a_body_trailer_hit_still_dedups(tmp_path, issue, make_tools):
    """Non-vacuity: the narrowed read still finds the issue it is supposed to find."""
    body = tmp_path / "b.md"
    body.write_text("B")
    owner = issue(3, fp=_FP)
    tools, calls = make_tools(Fakes(issues=[owner]))
    assert findings.main(_open_argv(body), tools) == 0
    assert [argv[:2] for argv in calls.gh] == [["issue", "comment"]]
