#!/usr/bin/env python3
"""Metamorphic property tests for the footgun guard: a wrapper never changes the verdict.

A command `problem()` refuses stays refused, with the same message, once it is joined to other
stages, prefixed by a shell keyword or wrapped in a command substitution, up to three wrappers
deep. A harmless command stays allowed under the same wrappers.

Run: uv run pytest .claude/hooks/tests/bash_guards/test_block_footguns_wrappers.py
"""

import importlib.util
import os
import sys

from hypothesis import given
from hypothesis import strategies as st

_HOOK = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "block-footguns.py",
)
sys.path.insert(0, os.path.dirname(_HOOK))
_spec = importlib.util.spec_from_file_location("block_footguns", _HOOK)
assert _spec and _spec.loader, "spec_from_file_location found no loader"
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

# One command per rule in `_RULES`; each rule judges a stage alone, reading no file and no cwd.
_DENIED = {
    "rollout_restart_problem": "kubectl rollout restart deploy/x",
    "remote_git_problem": "ssh daniel-pi 'git status'",
    "burst_public_hostname_problem": "ab -n 100 https://sonarr.daniel-hunter.com/",
    "issue_create_by_hand_problem": "gh issue create --title x",
    "tag_move_problem": "crane tag ghcr.io/o/a:sha-1 prod",
    "ci_dispatch_on_master_problem": "gh workflow run ci.yml --ref master",
}

_ALLOWED = (
    "kubectl rollout status deploy/x",
    "kubectl get pods -n homelab",
    "ssh daniel-server 'cd /home/ubuntu/server; git status'",
    "gh issue list",
    "ab -n 100 https://sonarr.local.daniel-hunter.com/",
    "echo hi",
)

_WRAPPERS = (
    "true ; {}",
    "true && {}",
    "false || {}",
    "echo ok | {}",
    "true\n{}",
    "{} && true",
    "echo $({})",
    'echo "$({})"',
    "echo `{}`",
    "! {}",
    "time {}",
    "command {}",
    "  {}  ",
)


def _wrap(command: str, wrappers: list[str]) -> str:
    for wrapper in wrappers:
        # A backtick inside backticks needs escaping, which no real command relies on.
        if "`" in wrapper and "`" in command:
            continue
        command = wrapper.format(command)
    return command


_WRAPPER_CHAINS = st.lists(st.sampled_from(_WRAPPERS), max_size=3)


def test_every_rule_has_a_denied_shape():
    assert set(_DENIED) == {rule.__name__ for rule in _mod._RULES}
    for command in _DENIED.values():
        assert _mod.problem(command), command


def test_a_footgun_in_a_command_substitution_is_refused():
    assert _mod.problem("echo $(kubectl rollout restart deploy/x)") == _mod.problem(
        "kubectl rollout restart deploy/x"
    )


def test_decide_denies_a_footgun_in_a_command_substitution():
    assert _mod.decide("echo `gh issue create --title x`")[0] == "deny"


def test_decide_asks_about_a_deploy_plane_write_in_a_command_substitution():
    assert _mod.decide("echo $(gh variable set DEPLOY_HOLD --body none)")[0] == "ask"


def test_a_footgun_in_single_quotes_is_only_text():
    assert _mod.problem("echo '$(kubectl rollout restart deploy/x)'") is None


@given(st.sampled_from(sorted(_DENIED.values())), _WRAPPER_CHAINS)
def test_a_wrapped_footgun_is_still_refused(bare, wrappers):
    assert _mod.problem(_wrap(bare, wrappers)) == _mod.problem(bare)


@given(st.sampled_from(_ALLOWED), _WRAPPER_CHAINS)
def test_a_wrapped_harmless_command_stays_allowed(command, wrappers):
    assert _mod.problem(_wrap(command, wrappers)) is None
