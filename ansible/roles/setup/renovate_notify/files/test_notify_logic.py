from datetime import datetime, timezone

import notify_logic as nl


def _pr(number=1, title="t", url="u", automerge=True, ci="success", conflicting=False):
    return nl.PR(number=number, title=title, url=url, automerge=automerge,
                 ci=ci, conflicting=conflicting)


# --- parse_automerge ---
def test_parse_automerge_enabled():
    assert nl.parse_automerge("🚦 **Automerge**: Enabled.") is True


def test_parse_automerge_disabled():
    assert nl.parse_automerge("🚦 **Automerge**: Disabled.") is False


def test_parse_automerge_absent_defaults_false():
    assert nl.parse_automerge("no marker here") is False
    assert nl.parse_automerge("") is False


# --- ci_rollup ---
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
    runs = [{"status": "in_progress", "conclusion": None},
            {"status": "completed", "conclusion": "failure"}]
    assert nl.ci_rollup(runs, []) == "failure"


def test_ci_rollup_neutral_and_skipped_are_ok():
    runs = [{"status": "completed", "conclusion": "neutral"},
            {"status": "completed", "conclusion": "skipped"}]
    assert nl.ci_rollup(runs, []) == "success"


# --- classify_pr ---
def test_classify_manual_when_automerge_disabled():
    assert nl.classify_pr(_pr(automerge=False, ci="success")) == "manual"


def test_classify_manual_even_if_failing():
    assert nl.classify_pr(_pr(automerge=False, ci="failure")) == "manual"


def test_classify_stuck_automerge_but_failing():
    assert nl.classify_pr(_pr(automerge=True, ci="failure")) == "stuck"


def test_classify_stuck_automerge_but_conflicting():
    assert nl.classify_pr(_pr(automerge=True, ci="success", conflicting=True)) == "stuck"


def test_classify_on_track_automerge_healthy():
    assert nl.classify_pr(_pr(automerge=True, ci="success")) == "on-track"


def test_classify_on_track_automerge_pending():
    assert nl.classify_pr(_pr(automerge=True, ci="pending")) == "on-track"


# --- actionable ---
def test_actionable_keeps_stuck_and_manual_drops_ontrack():
    prs = [
        _pr(number=8, automerge=True, ci="failure"),         # stuck
        _pr(number=9, automerge=False, ci="success"),        # manual
        _pr(number=12, automerge=True, ci="success"),        # on-track -> dropped
    ]
    out = nl.actionable(prs)
    assert [(pr.number, b) for pr, b in out] == [(8, "stuck"), (9, "manual")]


# --- fingerprint ---
def test_fingerprint_is_sorted_and_stable():
    a = [(_pr(number=9), "manual"), (_pr(number=8), "stuck")]
    b = [(_pr(number=8), "stuck"), (_pr(number=9), "manual")]
    assert nl.fingerprint(a) == nl.fingerprint(b) == "#8:stuck,#9:manual"


def test_fingerprint_empty_is_blank():
    assert nl.fingerprint([]) == ""


# --- should_notify ---
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


# --- render_digest ---
def test_render_digest_groups_and_links():
    items = [
        (_pr(number=8, title="container images", url="http://x/8",
             automerge=True, ci="failure"), "stuck"),
        (_pr(number=9, title="community.sops", url="http://x/9",
             automerge=False, ci="success"), "manual"),
    ]
    msg = nl.render_digest(items)
    assert "2 PR(s) need attention" in msg
    assert "#8 container images" in msg
    assert "http://x/8" in msg
    assert "Awaiting your merge" in msg
    assert "#9 community.sops" in msg


def test_render_digest_truncates_and_counts_overflow():
    items = [(_pr(number=i, title="x" * 80, url="http://x/%d" % i,
                  automerge=False, ci="success"), "manual") for i in range(60)]
    msg = nl.render_digest(items, limit=600)
    assert len(msg) <= 600
    assert "more" in msg


# --- dashboard_stale (Renovate fail-loud backstop) ---
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


# --- find_dashboard ---
def _issue(title, login="renovate[bot]", updated="2026-06-25T00:00:00Z", pr=False):
    d = {"title": title, "user": {"login": login}, "updated_at": updated}
    if pr:
        d["pull_request"] = {"url": "x"}
    return d


def test_find_dashboard_returns_updated_at():
    issues = [_issue("Some other issue"),
              _issue("Dependency Dashboard", updated="2026-06-24T09:00:00Z")]
    assert nl.find_dashboard(issues) == "2026-06-24T09:00:00Z"


def test_find_dashboard_skips_prs():
    # GitHub's issues endpoint also returns PRs; a PR titled like the dashboard is ignored.
    assert nl.find_dashboard([_issue("Dependency Dashboard", pr=True)]) is None


def test_find_dashboard_absent_returns_none():
    assert nl.find_dashboard([_issue("random")]) is None


def test_find_dashboard_ignores_non_renovate_author():
    # A human-created issue titled "Dependency Dashboard" must not be trusted as the dashboard.
    assert nl.find_dashboard([_issue("Dependency Dashboard", login="someuser")]) is None
