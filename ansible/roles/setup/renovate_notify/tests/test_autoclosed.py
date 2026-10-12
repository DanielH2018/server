"""The autoclosed arm (#4630): a Renovate PR closed unmerged while its update stays listed."""

import pathlib
from datetime import datetime, timezone

import notify_logic as nl

# The captured dashboard of 2026-09-09, whose Awaiting Schedule section names this branch.
_DASHBOARD_2026_09_09 = (
    pathlib.Path(__file__).resolve().parent / "dashboard_body_2026-09-09.txt"
).read_text()
_AWAITING_BRANCH = "renovate/lscr.io-linuxserver-code-server-4.x"
_NOW = datetime(2026, 9, 9, 13, 0, tzinfo=timezone.utc)


def _closed(
    number=4586,
    branch=_AWAITING_BRANCH,
    title="Update lscr.io/linuxserver/code-server Docker tag to v4.136.2-ls363 - autoclosed",
    closed_at="2026-09-09T12:00:00Z",
    merged_at=None,
    login="renovate[bot]",
):
    """A closed-pulls entry shaped like #4586's, the PR the issue measured."""
    return {
        "number": number,
        "title": title,
        "html_url": "https://github.com/o/r/pull/%d" % number,
        "head": {"ref": branch},
        "user": {"login": login},
        "closed_at": closed_at,
        "merged_at": merged_at,
    }


def test_dashboard_branches_reads_every_marker_in_the_captured_body():
    branches = nl.dashboard_branches(_DASHBOARD_2026_09_09)
    assert _AWAITING_BRANCH in branches  # unschedule-branch=, Awaiting Schedule
    # unpend-branch=, Pending Status Checks
    assert "renovate/k8s-image-nginx" in branches


def test_an_autoclosed_pr_whose_branch_awaits_on_the_dashboard_is_flagged():
    found = nl.find_autoclosed([_closed()], set(), _DASHBOARD_2026_09_09, now=_NOW)
    assert [(a.number, a.branch) for a in found] == [(4586, _AWAITING_BRANCH)]


def test_an_autoclosed_pr_whose_branch_left_the_dashboard_is_clean():
    """The update was landed some other way, as #4628 landed #4586's bump."""
    gone = _closed(branch="renovate/not-on-the-dashboard")
    assert nl.find_autoclosed([gone], set(), _DASHBOARD_2026_09_09, now=_NOW) == []


def test_an_autoclosed_pr_with_an_open_pr_on_its_branch_is_clean():
    found = nl.find_autoclosed(
        [_closed()], {_AWAITING_BRANCH}, _DASHBOARD_2026_09_09, now=_NOW
    )
    assert found == []


def test_an_autoclose_older_than_the_lookback_is_clean():
    """Renovate reuses the branch for the next update, which may legitimately await again."""
    old = _closed(closed_at="2026-08-30T12:00:00Z")
    assert nl.find_autoclosed([old], set(), _DASHBOARD_2026_09_09, now=_NOW) == []


def test_a_merged_or_hand_closed_pr_is_not_an_autoclose():
    merged = _closed(merged_at="2026-09-09T12:00:00Z")
    by_hand = _closed(
        title="Update lscr.io/linuxserver/code-server Docker tag to v4.136.2-ls363"
    )
    human = _closed(login="DanielH2018")
    found = nl.find_autoclosed(
        [merged, by_hand, human], set(), _DASHBOARD_2026_09_09, now=_NOW
    )
    assert found == []


def test_render_autoclosed_names_the_pr_without_the_suffix():
    found = nl.find_autoclosed([_closed()], set(), _DASHBOARD_2026_09_09, now=_NOW)
    text = nl.render_autoclosed(found)
    assert text.startswith(nl.AUTOCLOSED_HEADER)
    assert (
        "#4586 Update lscr.io/linuxserver/code-server Docker tag to v4.136.2-ls363 —"
        in text
    )
    assert "autoclosed —" not in text
    assert nl.autoclosed_fingerprint(found) == "#4586"
