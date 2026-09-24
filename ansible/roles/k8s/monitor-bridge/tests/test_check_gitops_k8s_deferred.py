"""GitOps Deploy — Status's sixth arm: a bump a broad tick deferred for lack of budget.

Split out of `test_check_gitops.py` at its 500-line cap; the `cfg` fixture is the suite's.
The arm exists because the deferring tick MERGED the bump — `behind_since` is empty, no later
tick's range carries it, and the defer-and-alert post named it exactly once (#2449).

Run: uv run pytest ansible/roles/k8s/monitor-bridge/tests/test_check_gitops_k8s_deferred.py
"""

import checks.gitops

# One line per deferred bump, "<origin_sha> <service> <unix_ts>", written by
# DeployerState.record_k8s_deferred.
_SONARR_DEFERRED = "abc123def4567890 sonarr 1000.0"


def test_a_freshly_deferred_bump_is_ok(cfg):
    """Ten minutes deferred is a tick that ran out of wall clock, not a fault.

    The next tick usually deploys it, so paging here would page on the ordinary case.
    """
    ok, msg = checks.gitops.gitops_status(
        cfg, None, None, None, now=1000.0 + 600, k8s_deferred=_SONARR_DEFERRED
    )
    assert ok
    assert msg == "no held deploy"


def test_a_bump_deferred_too_long_pages_and_names_its_two_commands(cfg):
    ok, msg = checks.gitops.gitops_status(
        cfg, None, None, None, now=1000.0 + 7 * 3600, k8s_deferred=_SONARR_DEFERRED
    )
    assert not ok
    assert "sonarr" in msg
    assert './scripts/deploy.sh --tags "sonarr"' in msg
    assert "clear-k8s-deferred sonarr" in msg


def test_the_oldest_deferred_bump_decides(cfg):
    """The threshold reads the oldest line, so a newer one cannot reset the clock."""
    marker = _SONARR_DEFERRED + "\nabc123def4567890 radarr 25000.0"
    ok, msg = checks.gitops.gitops_status(
        cfg, None, None, None, now=1000.0 + 7 * 3600, k8s_deferred=marker
    )
    assert not ok
    assert "radarr, sonarr" in msg


def test_an_unparseable_k8s_deferred_marker_is_ok(cfg):
    """A torn line reads as nothing pending, the rule every parser in `gitops_markers` holds.

    A page raised off it would name no service and print a clear command for nothing.
    """
    for marker in ("garbage", "sha sonarr not-a-number", "", "sha sonarr"):
        ok, msg = checks.gitops.gitops_status(
            cfg, None, None, None, now=1e9, k8s_deferred=marker
        )
        assert ok, marker
        assert msg == "no held deploy"


def test_a_held_deploy_is_reported_ahead_of_a_deferred_bump(cfg):
    """The hold arm names an actual failure; a deferred bump only waits on work."""
    ok, msg = checks.gitops.gitops_status(
        cfg,
        "held123abc456789",
        None,
        None,
        now=1e9,
        k8s_deferred=_SONARR_DEFERRED,
    )
    assert not ok
    assert "deploy held at" in msg
    assert "sonarr" not in msg
