"""The GitOps deploy pipeline's two watchdogs: is it alive, and is it stuck.

`gitops_alive` reads the tick's own heartbeat; `gitops_status` reads the deploy markers — a
hold, a diverged tree, or a tree parked behind origin. The behind arm exists because a deferred
BROAD change never fast-forwards: the host parks on an old tree while last_run keeps ticking
and is_diverged stays false. daniel-server ran a 12-commit-old tree for hours that way on
2026-08-02 with every GitOps signal green. A held BROAD apply also needs a different
remediation than a held service deploy, so the message says which it is.
"""

import time
from pathlib import Path

import pytest

import bridge_config
import checks_service

_REPO = Path(__file__).resolve().parents[5]


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
    result_ok, msg = checks_service.gitops_alive(age_s, max_age)
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
def test_gitops_status(hold, diverged, ok, must_contain, exact_msg):
    result_ok, msg = checks_service.gitops_status(hold, diverged)
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
        pytest.param(lambda: str(time.time()), True, (), id="fresh_file"),
        # 100m old > default 90m
        pytest.param(lambda: str(time.time() - 100 * 60), False, (), id="stale_file"),
        pytest.param(None, False, ("no last_run",), id="missing_file"),
        pytest.param(lambda: "not-a-float", False, ("unparseable",), id="unparseable"),
    ],
)
def test_check_gitops_alive(tmp_path, monkeypatch, content_fn, ok, must_contain):
    monkeypatch.setattr(bridge_config, "GITOPS_STATE_DIR", str(tmp_path))
    if content_fn is not None:
        _gw(tmp_path, "last_run", content_fn())
    result_ok, msg = checks_service.check_gitops_alive()
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
    tmp_path, monkeypatch, filename, content, ok, must_contain
):
    monkeypatch.setattr(bridge_config, "GITOPS_STATE_DIR", str(tmp_path))
    if filename is not None:
        _gw(tmp_path, filename, content)
    result_ok, msg = checks_service.check_gitops_status()
    assert result_ok is ok
    for s in must_contain:
        assert s in msg


def test_gitops_status_behind_briefly_is_ok():
    # A routine push leaves the host behind for one tick. That must never page.
    ok, msg = checks_service.gitops_status(
        None, None, "abc123def4567890 1000.0", now=1600.0
    )
    assert ok
    assert msg == "no held deploy"


def test_gitops_status_behind_too_long_pages():
    ok, msg = checks_service.gitops_status(
        None, None, "abc123def4567890 1000.0", now=1000.0 + 7 * 3600
    )
    assert not ok
    assert "behind origin" in msg
    assert "abc123de" in msg


def test_gitops_status_behind_respects_threshold_argument():
    ok, _ = checks_service.gitops_status(
        None, None, "abc123def4567890 1000.0", now=1000.0 + 120, max_behind_s=60
    )
    assert not ok


def test_gitops_status_hold_wins_over_behind():
    # A hold leaves the host behind too, but names the actual cause — report that, not the symptom.
    ok, msg = checks_service.gitops_status(
        "held123abc456789", None, "abc123def4567890 1.0", now=1e9
    )
    assert not ok
    assert "held" in msg


def test_gitops_status_diverged_wins_over_behind():
    ok, msg = checks_service.gitops_status(
        None, "div123abc4567890", "abc123def4567890 1.0", now=1e9
    )
    assert not ok
    assert "diverged" in msg


def test_gitops_status_unparseable_behind_marker_is_ok():
    # A garbled marker must read as "not behind" rather than page forever on garbage.
    for marker in ("garbage", "abc123 notanumber", "abc123", ""):
        ok, _ = checks_service.gitops_status(None, None, marker, now=1e9)
        assert ok, marker


def test_a_service_hold_names_the_pr():
    ok, msg = checks_service.gitops_status("deadbeefcafe")
    assert not ok
    assert "revert the offending PR" in msg


def test_a_plane_hold_names_the_playbook_instead():
    """The forward-only broad arm leaves the tree fast-forwarded with a playbook failed partway.

    Reverting the PR undoes none of that, so the message must name what to re-run instead --
    otherwise the monitor prescribes a remediation that cannot work.
    """
    ok, msg = checks_service.gitops_status(
        "deadbeefcafe", hold_plane="ansible/initial_setup.yml renovate_notify"
    )
    assert not ok
    assert "ansible/initial_setup.yml" in msg
    assert "revert the offending PR" not in msg


def test_a_plane_marker_without_a_hold_does_not_page():
    """hold_sha is still what decides.

    A stale hold_plane left behind by a cleared hold must not keep the monitor red on its own.
    """
    ok, _ = checks_service.gitops_status(None, hold_plane="ansible/deploy.yml")
    assert ok
