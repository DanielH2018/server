"""`shared_role_callers.py`: which tags' release records stand in for a shared role's (#2643)."""

import json
from pathlib import Path

import deploy_narrow
import shared_role_callers
from lib.repo_paths import REPO
from narrow_broad import Context


def _ctx(callers: dict[str, set[str]], declared: set[str]) -> Context:
    return Context(Path("."), "HEAD", declared, callers, lambda _m: None)


def test_a_caller_reached_through_another_shared_role_is_counted():
    """A first-hop-only walk would miss this, and then `longhorn-api`
    — called only by shared roles — could never discharge."""
    ctx = _ctx(
        {
            "manifests": {"web"},
            "longhorn-api": {"volume-revert"},
            "volume-revert": {"web"},
        },
        {"web"},
    )
    assert shared_role_callers.recorded_callers(["longhorn-api"], ctx) == {
        "longhorn-api": ["web"]
    }


def test_a_caller_that_never_runs_manifests_is_dropped():
    """It writes no release record, so requiring it would keep the line forever."""
    ctx = _ctx(
        {"manifests": {"web"}, "image-builder": {"web", "images"}}, {"web", "images"}
    )
    assert shared_role_callers.recorded_callers(["image-builder"], ctx) == {
        "image-builder": ["web"]
    }


def test_a_role_nobody_calls_reaches_no_tag():
    ctx = _ctx({"manifests": {"web"}}, {"web"})
    assert shared_role_callers.recorded_callers(["orphan"], ctx) == {"orphan": []}


def test_a_manifests_caller_no_tag_applies_leaves_the_other_writers_standing():
    """A role committed ahead of its entry calls `manifests` with no tag to apply it."""
    ctx = _ctx({"manifests": {"web", "new-role"}, "arr": {"web"}}, {"web"})
    assert shared_role_callers.recorded_callers(["arr"], ctx) == {"arr": ["web"]}


# DECIDED: this drives the real tree through the argv the deployer builds. The tick's fakes
# replace the subprocess, so only this sees a flag the CLI does not take, and arr-notification's
# two callers are the named members a broken caller walk would lose.
def test_the_deployers_argv_is_one_main_accepts(capsys):
    argv = deploy_narrow.shared_callers_argv({"arr-notification"})
    assert argv[4] == deploy_narrow.SHARED_CALLERS_SCRIPT
    assert shared_role_callers.main([*argv[5:], "--repo", str(REPO)]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "arr-notification": ["radarr", "sonarr"],
    }


# ── which tags deploy a shared role at all, record or no record (#2704) ──
def test_caller_tags_follows_a_shared_caller_and_skips_a_dead_end():
    callers = {"longhorn-api": {"volume-snapshot", "dead"}, "volume-snapshot": {"web"}}
    assert shared_role_callers.caller_tags("longhorn-api", {"web"}, callers) == {"web"}


def test_expand_shared_tags_keeps_order_and_leaves_an_unknown_name_as_typed():
    tags, replaced = shared_role_callers.expand_shared_tags(
        ["web", "helper", "typo"], {"web", "api"}, {"helper": {"api", "web"}}
    )
    assert tags == ["web", "api", "typo"]
    assert replaced == {"helper": ["api", "web"]}
