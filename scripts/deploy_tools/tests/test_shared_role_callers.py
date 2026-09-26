"""`shared_role_callers.py`: which tags' release records stand in for a shared role's (#2643)."""

import json
from pathlib import Path

import deploy_narrow
import shared_role_callers
from lib.repo_paths import REPO
from narrow_broad import Context


def _ctx(callers: dict[str, set[str]], declared: set[str]) -> Context:
    return Context(Path("."), "HEAD", declared, callers, lambda _m: None)


def _entry_tags(declared: set[str], **overrides: set[str]) -> dict[str, set[str]]:
    """`render_guard.entry_tags_at`'s answer for a fleet where only `overrides` set `tags:`."""
    return {name: overrides.get(name, {name}) for name in declared}


def test_a_caller_reached_through_another_shared_role_is_counted():
    """`land_tags.covered_roles` stops at the first hop; this must not, or `longhorn-api`
    — called only by shared roles — could never discharge."""
    ctx = _ctx(
        {
            "manifests": {"web"},
            "longhorn-api": {"volume-revert"},
            "volume-revert": {"web"},
        },
        {"web"},
    )
    assert shared_role_callers.recorded_callers(
        ["longhorn-api"], ctx, _entry_tags({"web"})
    ) == {"longhorn-api": ["web"]}


def test_a_caller_that_never_runs_manifests_is_dropped():
    """It writes no release record, so requiring it would keep the line forever."""
    ctx = _ctx(
        {"manifests": {"web"}, "image-builder": {"web", "images"}}, {"web", "images"}
    )
    assert shared_role_callers.recorded_callers(
        ["image-builder"], ctx, _entry_tags({"web", "images"})
    ) == {"image-builder": ["web"]}


def test_a_role_nobody_calls_reaches_no_tag():
    ctx = _ctx({"manifests": {"web"}}, {"web"})
    assert shared_role_callers.recorded_callers(
        ["orphan"], ctx, _entry_tags({"web"})
    ) == {"orphan": []}


# ── an entry's other declared tag is a recording caller the role graph cannot see (#2666) ──
def test_a_builder_entrys_other_tag_is_the_caller_whose_record_stands_in():
    """GREEN half. `images` renders no manifest, but its entry is also tagged `web`, so a
    `--tags web` deploy runs it and `web.json` records the run (#2666)."""
    ctx = _ctx({"manifests": {"web"}}, {"web", "images"})
    assert shared_role_callers.recorded_callers(
        ["images"], ctx, _entry_tags({"web", "images"}, images={"images", "web"})
    ) == {"images": ["web"]}


def test_an_entry_declaring_only_its_own_name_reaches_no_other_tag():
    """RED half. Drop the shared tag and the same role is back to no recording caller, so
    its line stands — the answer this file recorded before #2666."""
    ctx = _ctx({"manifests": {"web"}}, {"web", "images"})
    assert shared_role_callers.recorded_callers(
        ["images"], ctx, _entry_tags({"web", "images"})
    ) == {"images": []}


def test_a_merely_overlapping_entry_is_not_a_record_that_stands_in():
    """`web`'s own entry is also tagged `other`, so a `--tags other` deploy writes
    `web.json` without running `images`. Only a subset of `images`'s tags stands in."""
    ctx = _ctx({"manifests": {"web"}}, {"web", "images", "other"})
    entry_tags = _entry_tags(
        {"web", "images", "other"}, images={"images", "web"}, web={"web", "other"}
    )
    assert shared_role_callers.recorded_callers(["images"], ctx, entry_tags) == {
        "images": []
    }


# DECIDED: this drives the real tree through the argv the deployer builds. The tick's fakes
# replace the subprocess, so only this sees a flag the CLI does not take, and game-stats-lib's
# two callers are the named members a broken caller walk would lose.
def test_the_deployers_argv_is_one_main_accepts(capsys):
    argv = deploy_narrow.shared_callers_argv({"game-stats-lib", "n8n-images"})
    assert argv[4] == deploy_narrow.SHARED_CALLERS_SCRIPT
    assert shared_role_callers.main([*argv[5:], "--repo", str(REPO)]) == 0
    # `n8n-images` is the named member #2666 is about: it printed `[]` until this tree
    # expanded its entry's own `tags: [n8n-images, n8n]`.
    assert json.loads(capsys.readouterr().out) == {
        "game-stats-lib": ["terraria-stats", "valheim-stats"],
        "n8n-images": ["n8n"],
    }
