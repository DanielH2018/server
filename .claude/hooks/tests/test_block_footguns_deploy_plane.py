#!/usr/bin/env python3
"""Tests for `block-footguns` rule 8, the deploy plane's three verdicts.

Each verdict has a case table that runs through the hook's own `decide`, so a case proves the
dispatch as well as the rule. A tag move is denied, a write that clears `DEPLOY_HOLD` or
writes `PROD_DIGEST` asks the operator, and setting a hold or reading either variable passes.

Run: uv run pytest .claude/hooks
"""

import importlib.util
import os
import sys

import pytest

_HOOK = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "block-footguns.py"
)
sys.path.insert(0, os.path.dirname(_HOOK))
_spec = importlib.util.spec_from_file_location("block_footguns", _HOOK)
assert _spec and _spec.loader, "spec_from_file_location found no loader"
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_REPO = "-R DanielH2018/server"


@pytest.mark.parametrize(
    "command",
    [
        "flux tag artifact oci://ghcr.io/danielh2018/homelab-manifests:sha-1 --tag prod",
        "crane tag ghcr.io/danielh2018/homelab-manifests:sha-1 prod",
        "oras tag ghcr.io/danielh2018/homelab-manifests:sha-1 prod",
    ],
)
def test_a_tag_move_is_denied(command):
    decision, reason = _mod.decide(command)
    assert decision == "deny"
    assert "only the promote workflow's `tag` job moves it" in reason


@pytest.mark.parametrize(
    "command",
    [
        f"gh variable set DEPLOY_HOLD --body none {_REPO}",
        "gh variable set deploy_hold --body none",
        f"gh {_REPO} variable set --body none DEPLOY_HOLD",
        "gh variable set DEPLOY_HOLD --body=None",
        "gh variable set DEPLOY_HOLD -bnone",
        "gh variable set DEPLOY_HOLD --body ''",
        f"gh variable delete DEPLOY_HOLD {_REPO}",
    ],
)
def test_clearing_the_hold_asks(command):
    decision, reason = _mod.decide(command)
    assert decision == "ask"
    assert reason.startswith("This command clears DEPLOY_HOLD")


@pytest.mark.parametrize(
    "command",
    [
        "gh variable set DEPLOY_HOLD",
        "gh variable set DEPLOY_HOLD < hold.txt",
        'gh variable set DEPLOY_HOLD --body "$HOLD"',
        "gh variable set DEPLOY_HOLD -f vars.env DEPLOY_HOLD",
        "gh api -X PATCH repos/DanielH2018/server/actions/variables/DEPLOY_HOLD -f value=x",
        "gh api repos/DanielH2018/server/actions/variables -f name=DEPLOY_HOLD -f value=x",
        "gh api repos/DanielH2018/server/actions/variables --field=name=DEPLOY_HOLD",
        "gh api repos/DanielH2018/server/actions/variables --raw-field=name=deploy_hold",
    ],
)
def test_a_hold_write_whose_value_cannot_be_read_asks(command):
    decision, reason = _mod.decide(command)
    assert decision == "ask"
    assert "cannot read the value" in reason


@pytest.mark.parametrize(
    "command",
    [
        f"gh variable set PROD_DIGEST --body sha256:00 {_REPO}",
        "gh variable delete PROD_DIGEST",
        "gh api --method=DELETE repos/DanielH2018/server/actions/variables/PROD_DIGEST",
        "gh api repos/DanielH2018/server/actions/variables -fname=PROD_DIGEST -fvalue=x",
    ],
)
def test_a_prod_digest_write_asks(command):
    decision, reason = _mod.decide(command)
    assert decision == "ask"
    assert reason.startswith("PROD_DIGEST is written by the promote workflow")


@pytest.mark.parametrize(
    "command",
    [
        f"gh variable set DEPLOY_HOLD --body volume-revert {_REPO}",
        "gh variable set DEPLOY_HOLD --body=dr-restore",
        "gh variable set DEPLOY_HOLD -b'manual: migration window'",
        "gh variable set DEPLOY_HOLD --body none-until-friday",
        f"gh variable get DEPLOY_HOLD {_REPO}",
        f"gh variable list {_REPO}",
        "gh variable set SOME_OTHER --body none",
        "gh api repos/DanielH2018/server/actions/variables/DEPLOY_HOLD",
        "gh api -X GET repos/DanielH2018/server/actions/variables/PROD_DIGEST",
        "gh api -X GET repos/DanielH2018/server/actions/variables -fname=DEPLOY_HOLD",
        "flux push artifact oci://ghcr.io/o/a:sha-1 --path out",
        "crane digest ghcr.io/o/a:prod",
        "grep -rn 'gh variable set DEPLOY_HOLD --body none' docs",
    ],
)
def test_setting_a_hold_and_the_reads_pass(command):
    """The must-pass half: a literal reason sets a hold, which stops promotion, and every
    read or near miss stays out of the prompt."""
    assert _mod.decide(command) == (None, None)


def test_a_deny_in_one_stage_beats_an_ask_in_another():
    command = (
        "gh variable set DEPLOY_HOLD --body none && crane tag ghcr.io/o/a:sha-1 prod"
    )
    assert _mod.decide(command)[0] == "deny"
