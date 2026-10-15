"""`shared_role_callers.py`: which tags' deploys run a shared role."""

import json
import subprocess

import pytest

from deployer_lib import deploy_narrow
import shared_role_callers
from lib.repo_paths import REPO


# DECIDED: this drives the real tree through the argv the deployer builds. The tick's fakes
# replace the subprocess, so only this sees a flag the CLI does not take, and arr-notification's
# two callers are the named members a broken caller walk would lose.
def test_the_deployers_argv_is_one_main_accepts(capsys):
    argv = deploy_narrow.shared_callers_argv({"arr-notification"})
    assert argv[4] == deploy_narrow.SHARED_CALLERS_SCRIPT
    assert shared_role_callers.main([*argv[5:], "--repo", str(REPO)]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "callers": {"arr-notification": ["radarr", "sonarr"]},
        "owned_only": [],
    }


def test_the_deployers_decode_refuses_the_flat_object_an_older_checkout_prints(
    monkeypatch,
):
    """A deployer ahead of its checkout must keep the line, not read every role as owned."""
    flat = json.dumps({"arr-notification": ["radarr", "sonarr"]})
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **_kw: subprocess.CompletedProcess(argv, 0, flat, ""),
    )
    with pytest.raises(ValueError, match="no `callers` object"):
        deploy_narrow.shared_role_callers(str(REPO), {"arr-notification"})


# ── which tags deploy a shared role at all, record or no record ──
def test_caller_tags_follows_a_shared_caller_and_skips_a_dead_end():
    """A first-hop-only walk would miss `web`, so `longhorn-api` could never discharge."""
    callers = {"longhorn-api": {"volume-snapshot", "dead"}, "volume-snapshot": {"web"}}
    assert shared_role_callers.caller_tags("longhorn-api", {"web"}, callers) == {"web"}


def test_expand_shared_tags_keeps_order_and_leaves_an_unknown_name_as_typed():
    tags, replaced, _ = shared_role_callers.expand_shared_tags(
        ["web", "helper", "typo"], {"web", "api"}, {"helper": {"api", "web"}}
    )
    assert tags == ["web", "api", "typo"]
    assert replaced == {"helper": ["api", "web"]}
