"""The backlog cron's fetch reads three narrow slices, never the whole register.

`--state all` grows past its own `REGISTER_FETCH_TIMEOUT` (995 issues take 58.4s, and the
register grows about 350 issues a week), and a fetch that times out never reaches the
`ISSUE_LIST_CAP` warning that would explain it, so the backlog page would go silently stale
behind a bare `gh failed`. `backlog.render_markdown` only ever renders open findings and
closed `refuted` or `accepted` ones, so those three are what `load_backlog_issues` asks for.

`_findings_fakes` answers `issue list` from one list whatever the argv, which is exactly the
distinction under test here, so these tests answer gh themselves.
"""

from dev.findings_lib.boundaries import FindingsTools
from dev.findings_lib.gh_calls import SETTLED_LABELS, load_backlog_issues
from dev.findings_lib.issue_model import NO_REOPEN


def _tools(answer_for=lambda argv: []):
    """FindingsTools recording each `issue list` argv and answering from ``answer_for``."""
    seen: list[list[str]] = []

    def gh_json(*argv, **kwargs):
        seen.append(list(argv))
        return answer_for(list(argv))

    return FindingsTools(gh_json=gh_json), seen


def _query(argv: list[str]) -> tuple[str, tuple[str, ...]]:
    """The state and the non-`claude` labels one `issue list` argv asks for."""
    state = argv[argv.index("--state") + 1]
    labels = tuple(
        argv[i + 1]
        for i, a in enumerate(argv)
        if a == "--label" and argv[i + 1] != "claude"
    )
    return state, labels


def test_the_backlog_fetch_asks_only_for_the_rows_the_page_renders():
    tools, seen = _tools()
    load_backlog_issues(tools)
    assert [_query(argv) for argv in seen] == [
        ("open", ()),
        ("closed", ("refuted",)),
        ("closed", ("accepted",)),
    ]


def test_the_backlog_fetch_never_asks_for_the_whole_register():
    """The rejecting half: `--state all` must not be the fetch."""
    tools, seen = _tools()
    load_backlog_issues(tools)
    assert not [argv for argv in seen if _query(argv)[0] == "all"]


def test_an_issue_carrying_both_settled_labels_comes_back_once():
    """`refuted` and `accepted` are separate fetches, so a doubly-labelled issue hits two."""
    both = {"number": 7, "labels": [{"name": n} for n in ("claude", *SETTLED_LABELS)]}
    tools, _ = _tools(lambda argv: [] if _query(argv)[0] == "open" else [both])
    assert [i["number"] for i in load_backlog_issues(tools)] == [7]


def test_the_settled_labels_are_the_pair_that_refuses_a_reopen():
    """Non-vacuity: the fetch and `open`'s do-not-re-file rule read the same two labels.

    A third not-planned outcome added to `NO_REOPEN` alone would render in the settled table
    for anyone who fetched the whole register, and silently vanish from this one.
    """
    assert set(SETTLED_LABELS) == set(NO_REOPEN)
