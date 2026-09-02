import pathlib
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "files"))
import notify_logic as nl


def _pr(
    number=1,
    title="t",
    url="u",
    automerge=True,
    ci="success",
    conflicting=False,
    created_at="",
    dead_paths=None,
):
    return nl.PR(
        number=number,
        title=title,
        url=url,
        automerge=automerge,
        ci=ci,
        conflicting=conflicting,
        created_at=created_at,
        dead_paths=dead_paths,
    )


def test_parse_automerge_enabled():
    assert nl.parse_automerge("🚦 **Automerge**: Enabled.") is True


def test_parse_automerge_disabled():
    assert nl.parse_automerge("🚦 **Automerge**: Disabled.") is False


def test_parse_automerge_absent_defaults_false():
    assert nl.parse_automerge("no marker here") is False
    assert nl.parse_automerge("") is False


def test_ci_rollup_all_success():
    runs = [{"status": "completed", "conclusion": "success"}]
    statuses = [{"state": "success"}]
    assert nl.ci_rollup(runs, statuses) == "success"


def test_ci_rollup_failed_checkrun():
    runs = [{"status": "completed", "conclusion": "failure"}]
    assert nl.ci_rollup(runs, []) == "failure"


def test_ci_rollup_failed_legacy_status():
    # a failing commit-status (e.g. GitGuardian) with all check-runs green
    runs = [{"status": "completed", "conclusion": "success"}]
    statuses = [{"state": "failure"}]
    assert nl.ci_rollup(runs, statuses) == "failure"


def test_ci_rollup_pending_when_incomplete():
    runs = [{"status": "in_progress", "conclusion": None}]
    assert nl.ci_rollup(runs, []) == "pending"


def test_ci_rollup_pending_status_is_pending():
    # renovate/stability-days still soaking
    assert nl.ci_rollup([], [{"state": "pending"}]) == "pending"


def test_ci_rollup_failure_beats_pending():
    runs = [
        {"status": "in_progress", "conclusion": None},
        {"status": "completed", "conclusion": "failure"},
    ]
    assert nl.ci_rollup(runs, []) == "failure"


def test_ci_rollup_neutral_and_skipped_are_ok():
    runs = [
        {"status": "completed", "conclusion": "neutral"},
        {"status": "completed", "conclusion": "skipped"},
    ]
    assert nl.ci_rollup(runs, []) == "success"


def test_classify_manual_when_automerge_disabled():
    assert nl.classify_pr(_pr(automerge=False, ci="success")) == "manual"


def test_classify_manual_even_if_failing():
    assert nl.classify_pr(_pr(automerge=False, ci="failure")) == "manual"


def test_classify_stuck_automerge_but_failing():
    assert nl.classify_pr(_pr(automerge=True, ci="failure")) == "stuck"


def test_classify_stuck_automerge_but_conflicting():
    assert (
        nl.classify_pr(_pr(automerge=True, ci="success", conflicting=True)) == "stuck"
    )


def test_classify_on_track_automerge_healthy():
    assert nl.classify_pr(_pr(automerge=True, ci="success")) == "on-track"


def test_classify_on_track_automerge_pending():
    assert nl.classify_pr(_pr(automerge=True, ci="pending")) == "on-track"


def test_actionable_keeps_stuck_and_manual_drops_ontrack():
    prs = [
        _pr(number=8, automerge=True, ci="failure"),  # stuck
        _pr(number=9, automerge=False, ci="success"),  # manual
        _pr(number=12, automerge=True, ci="success"),  # on-track -> dropped
    ]
    out = nl.actionable(prs)
    assert [(pr.number, b) for pr, b in out] == [(8, "stuck"), (9, "manual")]


def test_fingerprint_is_sorted_and_stable():
    a = [(_pr(number=9), "manual"), (_pr(number=8), "stuck")]
    b = [(_pr(number=8), "stuck"), (_pr(number=9), "manual")]
    assert nl.fingerprint(a) == nl.fingerprint(b) == "#8:stuck,#9:manual"


def test_fingerprint_empty_is_blank():
    assert nl.fingerprint([]) == ""


# A `stuck` PR used to fingerprint on #number:bucket alone -> notify() fires once when it first
# goes stuck, then the fingerprint never changes again while the PR just sits there, so it never
# re-pages (PR #67, stuck since 2026-08-03, paged day 1 and went silent). The age dimension makes
# the fingerprint change — and so re-notify — each time the PR's stuck-age crosses a threshold.
# `manual` PRs deliberately carry no age dimension (nothing to escalate: they're waiting on a
# merge, not getting worse).
def test_fingerprint_stuck_pr_under_a_day_old_has_no_age_suffix():
    now = datetime(2026, 8, 10, tzinfo=timezone.utc)
    pr = _pr(number=67, created_at="2026-08-10T00:00:00Z")
    assert nl.fingerprint([(pr, "stuck")], now=now) == "#67:stuck"


def test_fingerprint_stuck_pr_crosses_1_day_threshold():
    now = datetime(2026, 8, 4, tzinfo=timezone.utc)
    pr = _pr(number=67, created_at="2026-08-03T00:00:00Z")
    assert nl.fingerprint([(pr, "stuck")], now=now) == "#67:stuck:1d"


def test_fingerprint_stuck_pr_crosses_3_day_threshold():
    now = datetime(2026, 8, 6, tzinfo=timezone.utc)
    pr = _pr(number=67, created_at="2026-08-03T00:00:00Z")
    assert nl.fingerprint([(pr, "stuck")], now=now) == "#67:stuck:3d"


def test_fingerprint_stuck_pr_crosses_7_and_14_day_thresholds():
    now = datetime(2026, 8, 10, tzinfo=timezone.utc)
    pr = _pr(number=67, created_at="2026-08-03T00:00:00Z")
    assert nl.fingerprint([(pr, "stuck")], now=now) == "#67:stuck:7d"
    now = datetime(2026, 8, 17, tzinfo=timezone.utc)
    assert nl.fingerprint([(pr, "stuck")], now=now) == "#67:stuck:14d"


def test_fingerprint_stuck_pr_missing_created_at_omits_age():
    # No timestamp available -> age unknown, same fingerprint as before this change.
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    pr = _pr(number=67, created_at="")
    assert nl.fingerprint([(pr, "stuck")], now=now) == "#67:stuck"


def test_fingerprint_manual_pr_never_gets_age_suffix():
    # `manual` PRs get no age dimension even when old — nothing to escalate on.
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    pr = _pr(number=9, automerge=False, created_at="2026-08-01T00:00:00Z")
    assert nl.fingerprint([(pr, "manual")], now=now) == "#9:manual"


def test_should_notify_repages_when_stuck_pr_crosses_age_threshold():
    # The escalation this closes: a stuck PR's fingerprint at day 1 differs from its fingerprint
    # at day 3, so should_notify fires again instead of staying silent forever after the day-1 page.
    day1 = nl.fingerprint(
        [(_pr(number=67, created_at="2026-08-03T00:00:00Z"), "stuck")],
        now=datetime(2026, 8, 4, tzinfo=timezone.utc),
    )
    day3 = nl.fingerprint(
        [(_pr(number=67, created_at="2026-08-03T00:00:00Z"), "stuck")],
        now=datetime(2026, 8, 6, tzinfo=timezone.utc),
    )
    assert day1 != day3
    assert nl.should_notify(day1, day3) == (True, "digest")


def test_should_notify_unchanged_is_silent():
    assert nl.should_notify("#8:stuck", "#8:stuck") == (False, "none")


def test_should_notify_new_backlog_is_digest():
    assert nl.should_notify("", "#8:stuck") == (True, "digest")


def test_should_notify_changed_backlog_is_digest():
    assert nl.should_notify("#8:stuck", "#8:stuck,#9:manual") == (True, "digest")


def test_should_notify_cleared_when_now_empty():
    assert nl.should_notify("#8:stuck", "") == (True, "cleared")


def test_should_notify_empty_to_empty_is_silent():
    assert nl.should_notify("", "") == (False, "none")


def test_render_digest_groups_and_links():
    items = [
        (
            _pr(
                number=8,
                title="container images",
                url="http://x/8",
                automerge=True,
                ci="failure",
            ),
            "stuck",
        ),
        (
            _pr(
                number=9,
                title="community.sops",
                url="http://x/9",
                automerge=False,
                ci="success",
            ),
            "manual",
        ),
    ]
    msg = nl.render_digest(items)
    assert "2 PR(s) need attention" in msg
    assert "#8 container images" in msg
    assert "http://x/8" in msg
    assert "Awaiting your merge" in msg
    assert "#9 community.sops" in msg


def test_render_digest_truncates_and_counts_overflow():
    items = [
        (
            _pr(
                number=i,
                title="x" * 80,
                url="http://x/%d" % i,
                automerge=False,
                ci="success",
            ),
            "manual",
        )
        for i in range(60)
    ]
    msg = nl.render_digest(items, limit=600)
    assert len(msg) <= 600
    assert "more" in msg


# Renovate rewrites its Dependency Dashboard issue every run (~daily here). A stale or
# absent dashboard means the Renovate App/config is broken — and in that state there are
# no PRs, so the PR digest alone reads as a healthy "backlog cleared". This is the gap.
def test_dashboard_stale_fresh_is_false():
    now = datetime(2026, 6, 26, tzinfo=timezone.utc)
    assert nl.dashboard_stale("2026-06-25T12:00:00Z", now=now) is False


def test_dashboard_stale_old_is_true():
    now = datetime(2026, 6, 26, tzinfo=timezone.utc)
    assert nl.dashboard_stale("2026-06-15T12:00:00Z", now=now) is True


def test_dashboard_stale_absent_is_true():
    # No dashboard issue at all (Renovate App uninstalled / never created it).
    assert nl.dashboard_stale(None) is True


def test_dashboard_stale_boundary_not_yet_stale():
    # Exactly the threshold age (8d): age_days > max is False, so not yet stale.
    now = datetime(2026, 6, 26, tzinfo=timezone.utc)
    assert nl.dashboard_stale("2026-06-18T00:00:00Z", now=now) is False


def _issue(title, login="renovate[bot]", updated="2026-06-25T00:00:00Z", pr=False):
    d = {"title": title, "user": {"login": login}, "updated_at": updated}
    if pr:
        d["pull_request"] = {"url": "x"}
    return d


def test_find_dashboard_returns_updated_at():
    issues = [
        _issue("Some other issue"),
        _issue("Dependency Dashboard", updated="2026-06-24T09:00:00Z"),
    ]
    assert nl.find_dashboard(issues) == "2026-06-24T09:00:00Z"


def test_find_dashboard_skips_prs():
    # GitHub's issues endpoint also returns PRs; a PR titled like the dashboard is ignored.
    assert nl.find_dashboard([_issue("Dependency Dashboard", pr=True)]) is None


def test_find_dashboard_absent_returns_none():
    assert nl.find_dashboard([_issue("random")]) is None


def test_find_dashboard_ignores_non_renovate_author():
    # A human-created issue titled "Dependency Dashboard" must not be trusted as the dashboard.
    assert nl.find_dashboard([_issue("Dependency Dashboard", login="someuser")]) is None


# A package whose lookup starts failing (karakeep's gcr.io image, 2026-08) gets no PR and
# doesn't touch dashboard staleness — the dashboard still updates on schedule. This section
# is the only signal, so it must be parsed into the notify path or it silently stops
# receiving updates forever.
_PROBLEMS_BODY = """This issue lists Renovate updates and detected dependencies.

## Repository Problems

These problems occurred while renovating this repository.

 - `WARN: Failed to look up docker package ghcr.io/karakeep-app/karakeep`
 - `WARN: Invalid schedule: "on the last day of the month"`

## Detected Dependencies

some other section
"""


def test_parse_repository_problems_section_present():
    problems = nl.parse_repository_problems(_PROBLEMS_BODY)
    assert problems == {
        "WARN: Failed to look up docker package ghcr.io/karakeep-app/karakeep",
        'WARN: Invalid schedule: "on the last day of the month"',
    }


def test_parse_repository_problems_section_absent_is_empty():
    assert nl.parse_repository_problems("no problems section here") == set()
    assert nl.parse_repository_problems("") == set()


def test_find_dashboard_problems_populates_from_dashboard_body():
    issues = [_issue("Dependency Dashboard")]
    issues[0]["body"] = _PROBLEMS_BODY
    assert nl.find_dashboard_problems(issues) == {
        "WARN: Failed to look up docker package ghcr.io/karakeep-app/karakeep",
        'WARN: Invalid schedule: "on the last day of the month"',
    }


def test_find_dashboard_problems_no_dashboard_is_empty():
    assert nl.find_dashboard_problems([_issue("random")]) == set()


def test_find_dashboard_problems_dashboard_without_section_is_empty():
    issues = [_issue("Dependency Dashboard")]
    issues[0]["body"] = "Nothing wrong here.\n\n## Detected Dependencies\n"
    assert nl.find_dashboard_problems(issues) == set()


def test_problems_fingerprint_sorted_and_stable():
    a = {"z problem", "a problem"}
    b = {"a problem", "z problem"}
    assert (
        nl.problems_fingerprint(a)
        == nl.problems_fingerprint(b)
        == "a problem,z problem"
    )


def test_problems_fingerprint_empty_is_blank():
    assert nl.problems_fingerprint(set()) == ""


def test_should_notify_unchanged_problems_is_silent():
    fp = "|problems:" + nl.problems_fingerprint({"WARN: lookup failed for karakeep"})
    assert nl.should_notify(fp, fp) == (False, "none")


def test_should_notify_new_problem_pages():
    prev = "|problems:" + nl.problems_fingerprint({"WARN: lookup failed for karakeep"})
    cur = "|problems:" + nl.problems_fingerprint(
        {"WARN: lookup failed for karakeep", "WARN: invalid schedule"}
    )
    assert prev != cur
    notify, kind = nl.should_notify(prev, cur)
    assert notify is True
    assert kind == "digest"


def test_should_notify_problems_first_appearing_pages():
    cur = "|problems:" + nl.problems_fingerprint({"WARN: lookup failed for karakeep"})
    notify, kind = nl.should_notify("", cur)
    assert notify is True
    assert kind == "digest"


def test_render_problems_lists_each_problem():
    msg = nl.render_problems({"WARN: lookup failed for karakeep"})
    assert "Repository Problems" in msg
    assert "WARN: lookup failed for karakeep" in msg


#
# Renovate holds one branch per branchName, so a branch conflicting against a DELETED path blocks
# the dependency it tracks from ever producing a mergeable PR — while the dashboard keeps detecting
# the update at the live path and the PR reports only as "conflicting", which reads as ordinary
# rebase noise. Two occurrences: #67/#42/#69 (compose templates archived by the k3s migration) and
# #41 (roles/containers/karakeep, same cutover), the second found on 2026-08-20 with the live pin
# 24 days behind.

_GONE = ("ansible/roles/containers/karakeep/templates/docker-compose.yml.j2",)


def test_dead_path_pr_is_its_own_bucket():
    """Not "stuck" and not "manual" — the remedy differs: close it, do not rebase it."""
    assert (
        nl.classify_pr(_pr(automerge=False, conflicting=True, dead_paths=_GONE))
        == "dead-path"
    )


def test_dead_path_beats_automerge_state():
    """A dead-path PR needs a human whether or not automerge was ever enabled."""
    for automerge in (True, False):
        pr = _pr(automerge=automerge, conflicting=True, dead_paths=_GONE)
        assert nl.classify_pr(pr) == "dead-path"


def test_a_conflicting_pr_with_live_files_is_still_just_stuck():
    """An ordinary conflict a rebase resolves must not be mislabelled as unrecoverable."""
    pr = _pr(automerge=True, conflicting=True, dead_paths=())
    assert nl.classify_pr(pr) == "stuck"
    assert "deleted path" not in nl._pr_note(pr)


def test_unlooked_up_is_not_treated_as_dead():
    """None means "not checked" and must never be read as "nothing exists"."""
    pr = _pr(automerge=True, conflicting=True, dead_paths=None)
    assert nl.classify_pr(pr) == "stuck"


def test_a_clean_pr_is_never_dead_path():
    """The dead-path question only arises for a conflicting PR."""
    assert (
        nl.classify_pr(_pr(automerge=True, conflicting=False, dead_paths=()))
        == "on-track"
    )


def test_dead_path_note_names_the_files_and_the_remedy():
    """ "Conflicting" implies a rebase will fix it; here nothing will, so say so."""
    note = nl._pr_note(_pr(conflicting=True, dead_paths=_GONE))
    assert "deleted path" in note
    assert "close it" in note.lower()
    assert "docker-compose.yml.j2" in note


def test_dead_path_prs_reach_the_digest():
    """A bucket nothing surfaces is the same silence the check was written to end."""
    pr = _pr(automerge=False, conflicting=True, dead_paths=_GONE)
    assert (pr, "dead-path") in nl.actionable([pr])
