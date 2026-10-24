"""The block-footguns stale-probe rule: a probe gate run from a worktree behind origin/master.

The fixture is a real repo: a primary checkout, a linked worktree made from it, and an
`origin/master` ref moved past the worktree's HEAD. The rule reads git, so a fake of it would
leave the merge-base arithmetic untested.

Run: uv run pytest .claude/hooks
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

from lib.git_testing import commit, git, init_repo

_HOOK = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "block-footguns.py"
)
sys.path.insert(0, os.path.dirname(_HOOK))
_spec = importlib.util.spec_from_file_location("block_footguns", _HOOK)
assert _spec and _spec.loader, "spec_from_file_location found no loader"
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
import _stale_probe_rule as _rule  # noqa: E402  (needs the hooks directory on sys.path)

_ROLE_FILE = "ansible/roles/k8s/metallb/defaults/main.yml"
_GATE = "uv run python scripts/diagnostics/probe.py health metallb"


@pytest.fixture
def repo(tmp_path):
    """(primary, worktree): the worktree branched at the second commit; origin/master sits there."""
    primary = init_repo(tmp_path / "primary")
    commit(primary, "probe", **{"scripts/diagnostics/probe.py": ""})
    first = commit(primary, "role", **{_ROLE_FILE: "replicas: 1\n"})
    worktree = tmp_path / "wt"
    git(primary, "worktree", "add", "-q", "-b", "orchestrator", str(worktree), first)
    git(primary, "update-ref", "refs/remotes/origin/master", first)
    return primary, worktree


def _move_origin(primary: Path, path: str, text: str) -> str:
    sha = commit(primary, path, **{path: text})
    git(primary, "update-ref", "refs/remotes/origin/master", sha)
    return sha


@pytest.mark.parametrize("subcommand", ["health", "takeover"])
def test_a_gate_from_a_worktree_behind_a_role_change_is_flagged(repo, subcommand):
    primary, worktree = repo
    _move_origin(primary, _ROLE_FILE, "replicas: 2\n")
    gate = f"uv run python scripts/diagnostics/probe.py {subcommand} metallb"
    reason = _rule.stale_probe_problem(gate.split(), worktree)
    assert reason and _ROLE_FILE in reason
    assert f"cd {primary} && {gate}" in reason


def test_a_gate_from_a_current_worktree_is_clean(repo):
    _, worktree = repo
    assert _rule.stale_probe_problem(_GATE.split(), worktree) is None


def test_a_worktree_holding_masters_content_after_a_squash_is_clean(repo):
    """The branch lacks the squash commit but carries the same role file."""
    primary, worktree = repo
    _move_origin(primary, _ROLE_FILE, "replicas: 2\n")
    commit(worktree, "squash", **{_ROLE_FILE: "replicas: 2\n"})
    assert _rule.stale_probe_problem(_GATE.split(), worktree) is None


def test_a_master_change_outside_what_the_probe_reads_is_clean(repo):
    primary, worktree = repo
    _move_origin(primary, "docs/landing.md", "prose\n")
    assert _rule.stale_probe_problem(_GATE.split(), worktree) is None


def test_the_primary_checkout_behind_origin_is_clean(repo):
    """The deployer applies the primary's HEAD, so its tree is what the cluster runs."""
    primary, _ = repo
    _move_origin(primary, _ROLE_FILE, "replicas: 2\n")
    git(primary, "reset", "-q", "--hard", "HEAD~1")
    assert _rule.stale_probe_problem(_GATE.split(), primary) is None


def test_the_primary_probe_named_from_a_stale_worktree_is_clean(repo):
    """The probe reads the tree its own file sits in, not the shell's cwd."""
    primary, worktree = repo
    _move_origin(primary, _ROLE_FILE, "replicas: 2\n")
    words = [
        "uv",
        "run",
        "python",
        f"{primary}/scripts/diagnostics/probe.py",
        "health",
        "x",
    ]
    assert _rule.stale_probe_problem(words, worktree) is None


@pytest.mark.parametrize(
    "command",
    [
        "uv run python scripts/diagnostics/probe.py monitors",
        "grep -n health scripts/diagnostics/probe.py",
    ],
)
def test_a_non_gate_stage_from_a_stale_worktree_is_clean(repo, command):
    primary, worktree = repo
    _move_origin(primary, _ROLE_FILE, "replicas: 2\n")
    assert _rule.stale_probe_problem(command.split(), worktree) is None


def test_the_hook_denies_the_stale_gate_end_to_end(repo):
    """`decide` reaches the rule with the payload's cwd, after a `cd` stage."""
    primary, worktree = repo
    _move_origin(primary, _ROLE_FILE, "replicas: 2\n")
    verdict, reason = _mod.decide(f"cd {worktree} && {_GATE}", cwd=str(primary))
    assert verdict == "deny" and "#6110" in reason
