"""The GitOps deploy pipeline's two watchdogs: is it alive, and is it stuck.

`gitops_alive` reads the tick's own heartbeat; `gitops_status` reads the deploy markers — a
hold, a diverged tree, or a tree parked behind origin. The behind arm exists because a deferred
BROAD change never fast-forwards: the host parks on an old tree while last_run keeps ticking
and is_diverged stays false. daniel-server ran a 12-commit-old tree for hours that way on
2026-08-02 with every GitOps signal green. A held BROAD apply also needs a different
remediation than a held service deploy, so the message says which it is.
"""

from dataclasses import replace

import pytest

import checks.gitops

# The last_run marker is written against this epoch and the check reads the same one, so
# "fresh" and "stale" are exact distances from the 90m default rather than a race with the
# suite's own runtime (#2158).
GITOPS_NOW = 1_780_000_000.0


@pytest.mark.parametrize(
    ("age_s", "max_age", "ok", "must_contain"),
    [
        pytest.param(60, 5400, True, ("1m ago",), id="fresh"),
        # exactly at max age still counts as alive (<=)
        pytest.param(5400, 5400, True, (), id="at_threshold_is_ok"),
        pytest.param(6000, 5400, False, ("100m ago",), id="stale"),  # 100m > 90m
    ],
)
def test_gitops_alive(age_s, max_age, ok, must_contain):
    result_ok, msg = checks.gitops.gitops_alive(age_s, max_age)
    assert result_ok is ok
    for s in must_contain:
        assert s in msg


@pytest.mark.parametrize(
    ("hold", "diverged", "ok", "must_contain", "exact_msg"),
    [
        pytest.param(None, None, True, (), "no held deploy", id="no_hold"),
        pytest.param("", None, True, (), None, id="empty_is_ok"),
        pytest.param(
            "abc123def4567890", None, False, ("abc123de",), None, id="held_names_sha"
        ),
        pytest.param(
            None,
            "def456abc7890123",
            False,
            ("diverged", "def456ab"),
            None,
            id="diverged_names_sha",
        ),
        pytest.param(
            "abc123def4567890",
            "def456abc7890123",
            False,
            ("held",),
            None,
            id="hold_takes_priority_over_diverged",
        ),
    ],
)
def test_gitops_status(hold, diverged, ok, must_contain, exact_msg, cfg):
    result_ok, msg = checks.gitops.gitops_status(cfg, hold, diverged)
    assert result_ok is ok
    if exact_msg is not None:
        assert msg == exact_msg
    for s in must_contain:
        assert s in msg


def _gw(tmp_path, name, content):
    (tmp_path / name).write_text(content)


@pytest.mark.parametrize(
    ("content_fn", "ok", "must_contain"),
    [
        pytest.param(lambda: str(GITOPS_NOW), True, (), id="fresh_file"),
        # 100m old > default 90m
        pytest.param(lambda: str(GITOPS_NOW - 100 * 60), False, (), id="stale_file"),
        pytest.param(None, False, ("no last_run",), id="missing_file"),
        pytest.param(lambda: "not-a-float", False, ("unparseable",), id="unparseable"),
    ],
)
def test_check_gitops_alive(tmp_path, monkeypatch, content_fn, ok, must_contain, cfg):
    cfg = replace(cfg, GITOPS_STATE_DIR=str(tmp_path))
    if content_fn is not None:
        _gw(tmp_path, "last_run", content_fn())
    result_ok, msg = checks.gitops.check_gitops_alive(cfg, now=GITOPS_NOW)
    assert result_ok is ok
    for s in must_contain:
        assert s in msg


@pytest.mark.parametrize(
    ("filename", "content", "ok", "must_contain"),
    [
        pytest.param(None, None, True, (), id="no_file_is_ok"),
        pytest.param("hold_sha", "abc123def4567890", False, ("abc123de",), id="held"),
        pytest.param(
            "diverged_sha", "def456abc7890123", False, ("diverged",), id="diverged"
        ),
    ],
)
def test_check_gitops_status(
    tmp_path, monkeypatch, filename, content, ok, must_contain, cfg
):
    cfg = replace(cfg, GITOPS_STATE_DIR=str(tmp_path))
    if filename is not None:
        _gw(tmp_path, filename, content)
    result_ok, msg = checks.gitops.check_gitops_status(cfg)
    assert result_ok is ok
    for s in must_contain:
        assert s in msg


def test_gitops_status_behind_briefly_is_ok(cfg):
    # A routine push leaves the host behind for one tick. That must never page.
    ok, msg = checks.gitops.gitops_status(
        cfg, None, None, "abc123def4567890 1000.0", now=1600.0
    )
    assert ok
    assert msg == "no held deploy"


def test_gitops_status_behind_too_long_pages(cfg):
    ok, msg = checks.gitops.gitops_status(
        cfg, None, None, "abc123def4567890 1000.0", now=1000.0 + 7 * 3600
    )
    assert not ok
    assert "behind origin" in msg
    assert "abc123de" in msg


def test_gitops_status_behind_respects_threshold_argument(cfg):
    ok, _ = checks.gitops.gitops_status(
        cfg, None, None, "abc123def4567890 1000.0", now=1000.0 + 120, max_behind_s=60
    )
    assert not ok


def test_gitops_status_hold_wins_over_behind(cfg):
    # A hold leaves the host behind too, but names the actual cause — report that, not the symptom.
    ok, msg = checks.gitops.gitops_status(
        cfg, "held123abc456789", None, "abc123def4567890 1.0", now=1e9
    )
    assert not ok
    assert "held" in msg


def test_gitops_status_diverged_wins_over_behind(cfg):
    ok, msg = checks.gitops.gitops_status(
        cfg, None, "div123abc4567890", "abc123def4567890 1.0", now=1e9
    )
    assert not ok
    assert "diverged" in msg


def test_gitops_status_unparseable_behind_marker_is_ok(cfg):
    # A garbled marker must read as "not behind" rather than page forever on garbage.
    for marker in ("garbage", "abc123 notanumber", "abc123", ""):
        ok, _ = checks.gitops.gitops_status(cfg, None, None, marker, now=1e9)
        assert ok, marker


def test_a_service_hold_names_the_pr(cfg):
    ok, msg = checks.gitops.gitops_status(cfg, "deadbeefcafe")
    assert not ok
    assert "revert the offending PR" in msg


def test_a_plane_hold_names_the_playbook_instead(cfg):
    """The forward-only broad arm leaves the tree fast-forwarded with a playbook failed partway.

    Reverting the PR undoes none of that, so the message must name what to re-run instead --
    otherwise the monitor prescribes a remediation that cannot work.
    """
    ok, msg = checks.gitops.gitops_status(
        cfg, "deadbeefcafe", hold_plane="ansible/initial_setup.yml renovate_notify"
    )
    assert not ok
    assert "ansible/initial_setup.yml" in msg
    assert "revert the offending PR" not in msg


def test_a_plane_marker_without_a_hold_does_not_page(cfg):
    """hold_sha is still what decides.

    A stale hold_plane left behind by a cleared hold must not keep the monitor red on its own.
    """
    ok, _ = checks.gitops.gitops_status(cfg, None, hold_plane="ansible/deploy.yml")
    assert ok


# ── the manual_plane marker: a setup role the deployer fast-forwarded past ──────────────
# One line per pending role, "<origin_sha> <playbook> <role> <unix_ts>", written by
# DeployerState.record_manual_plane.
_K3S_PENDING = "abc123def4567890 ansible/k3s-bringup.yml k3s 1000.0"


def test_a_freshly_pending_role_is_ok(cfg):
    """A few hours pending is the normal state after the tick merges such a range.

    Paging on it immediately would page on every one of those merges.
    """
    ok, msg = checks.gitops.gitops_status(
        cfg, None, None, None, now=1000.0 + 600, manual_plane=_K3S_PENDING
    )
    assert ok
    assert msg == "no held deploy"


def test_a_role_pending_too_long_pages_and_names_it(cfg):
    ok, msg = checks.gitops.gitops_status(
        cfg, None, None, None, now=1000.0 + 7 * 3600, manual_plane=_K3S_PENDING
    )
    assert not ok
    assert "k3s" in msg
    assert "clear-manual-plane" in msg


def test_the_oldest_pending_role_decides(cfg):
    """The threshold is read against the OLDEST line.

    A role recorded this minute must not reset the clock on one that has waited all day.
    """
    marker = _K3S_PENDING + "\ndef456abc7890123 none common 25000.0"
    ok, msg = checks.gitops.gitops_status(
        cfg, None, None, None, now=1000.0 + 7 * 3600, manual_plane=marker
    )
    assert not ok
    assert "common" in msg and "k3s" in msg


def test_a_narrowed_row_pages_the_clear_that_names_what_it_applied(cfg):
    """The page prints the same clear the banner does, not a bare `<role>` (#2349).

    A bare clear after a narrowed apply drops a tag a later range added to the row.
    `common`'s empty row gets the bare form, since `--applied` there keeps the line.
    """
    marker = _K3S_PENDING + "\ndef456abc7890123 none common 25000.0"
    ok, msg = checks.gitops.gitops_status(
        cfg,
        None,
        now=1000.0 + 7 * 3600,
        manual_plane=marker,
        manual_plane_tags="common -\nk3s kubeconfig",
    )
    assert not ok
    assert "clear-manual-plane common && " in msg
    assert msg.endswith("clear-manual-plane k3s --applied kubeconfig`")
    assert "<role>" not in msg


def test_a_role_with_no_row_pages_the_bare_clear(cfg):
    """The rejecting half: nothing narrowed, so the whole-role clear."""
    ok, msg = checks.gitops.gitops_status(
        cfg, None, now=1000.0 + 7 * 3600, manual_plane=_K3S_PENDING
    )
    assert not ok
    assert msg.endswith("clear-manual-plane k3s`")


def test_an_unparseable_manual_plane_marker_is_ok(cfg):
    """Same rule as `behind_since`: garbage must not page forever with nothing to clear."""
    for marker in ("garbage", "a b c notanumber", "", "a b c"):
        ok, _ = checks.gitops.gitops_status(
            cfg, None, None, None, now=1e9, manual_plane=marker
        )
        assert ok, marker


def test_a_hold_wins_over_a_pending_role(cfg):
    """A hold names a broken apply; a pending role names work nobody has started yet."""
    ok, msg = checks.gitops.gitops_status(
        cfg, "held123abc456789", None, None, now=1e9, manual_plane=_K3S_PENDING
    )
    assert not ok
    assert "held" in msg
    assert "clear-manual-plane" not in msg


def test_a_stale_behind_marker_wins_over_a_pending_role(cfg):
    """Both are stale at once when a bring-up playbook parks behind an already-recorded role.

    They are independent faults, so specificity cannot order them and urgency does: behind
    means the deployer has stopped and every other session's landing exits 4 from deploy.sh
    until a hand pulls the primary checkout. A pending role blocks nobody.
    """
    ok, msg = checks.gitops.gitops_status(
        cfg,
        None,
        None,
        "abc123def4567890 1000.0",
        now=1000.0 + 7 * 3600,
        manual_plane=_K3S_PENDING,
    )
    assert not ok
    assert "behind origin" in msg
    assert "clear-manual-plane" not in msg


def test_check_gitops_status_reads_the_manual_plane_file(tmp_path, cfg):
    """The marker is read off the same :ro state mount as `behind_since`."""
    cfg = replace(cfg, GITOPS_STATE_DIR=str(tmp_path))
    _gw(tmp_path, "manual_plane", "abc123def4567890 ansible/k3s-bringup.yml k3s 1.0")
    _gw(tmp_path, "manual_plane_tags", "k3s kubeconfig")
    ok, msg = checks.gitops.check_gitops_status(cfg)
    assert not ok
    assert "k3s --applied kubeconfig" in msg, "the tags row is read off the mount too"


# ── consecutive ticks deferred on a busy service lock (issue #1847) ───────────────────────────
_CONTENTION = "abc123def4567890 sonarr 1000.0 1900.0 2"


def test_a_short_contention_streak_is_ok(cfg):
    """CLEAN half: one operator deploy holding a lock for a few minutes is not a fault."""
    ok, _ = checks.gitops.gitops_status(
        cfg, None, contention_since=_CONTENTION, now=1000.0 + 20 * 60
    )
    assert ok


def test_a_long_contention_streak_pages_naming_the_lock(cfg):
    """FLAGGED half: the streak is older than the deployer's longest apply budget."""
    ok, msg = checks.gitops.gitops_status(
        cfg, None, contention_since=_CONTENTION, now=1000.0 + 31 * 60
    )
    assert not ok
    assert "service lock sonarr" in msg
    assert "2 consecutive" in msg
    assert "clear-contention" in msg


def test_contention_respects_threshold_argument(cfg):
    ok, _ = checks.gitops.gitops_status(
        cfg, None, contention_since=_CONTENTION, now=1000.0 + 120, max_contention_s=60
    )
    assert not ok


def test_an_unparseable_contention_marker_is_ok(cfg):
    ok, _ = checks.gitops.gitops_status(
        cfg, None, contention_since="abc123 sonarr not-a-stamp 1 1", now=1e9
    )
    assert ok


def test_a_stale_contention_streak_is_reported_ahead_of_behind(cfg):
    """The streak names the cause; behind names the symptom the same fault produces."""
    ok, msg = checks.gitops.gitops_status(
        cfg,
        None,
        None,
        "abc123def4567890 1000.0",
        now=1000.0 + 7 * 3600,
        contention_since=_CONTENTION,
    )
    assert not ok
    assert "service lock" in msg


def test_hold_wins_over_contention(cfg):
    ok, msg = checks.gitops.gitops_status(
        cfg, "abc123def4567890", contention_since=_CONTENTION, now=1e9
    )
    assert not ok
    assert "held" in msg


def test_check_gitops_status_reads_the_contention_file(tmp_path, cfg):
    cfg = replace(cfg, GITOPS_STATE_DIR=str(tmp_path))
    _gw(tmp_path, "contention_since", "abc123def4567890 all 1.0 1.0 1")
    ok, msg = checks.gitops.check_gitops_status(cfg)
    assert not ok
    assert "service lock all" in msg
