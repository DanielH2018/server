#!/usr/bin/env python3
"""The two operations `deploy.sh` runs under the tree lock must each have a bound (issue #1845).

ADR-0017 argues the tree-lock hold is "seconds" -- long enough to copy HEAD into a snapshot
and no longer -- and the deployer's `TimeoutStartSec` budget assumes it. Two calls inside that
hold had no bound in code: the full-run tag enumeration (`deploy_tags.py list` via `uv run`,
which a cold `uv sync` can stretch to minutes) and the reaper's per-directory worktree removal
(a root left with hundreds of dead snapshots). Each now has one, and each is proved here to
fire: a bound that is only ever observed not firing is indistinguishable from no bound.

Run: uv run pytest scripts/deploy_tools/tests/test_deploy_tree_lock_hold_is_bounded.py
"""

import subprocess
from pathlib import Path

from _deploy_sh_fakes import (
    FAKE_RECAP,
    FLOCK_STUB,
    UV_WRAPPER_ARMS,
    deploy_sh_env,
    make_snapshot_repo,
)

_REPO = Path(__file__).resolve().parents[3]
_DEPLOY_SH = _REPO / "scripts" / "deploy.sh"
_SNAPSHOT_FAILED = 77

# `exec sleep`, not `sleep`: `timeout` signals the stub, and a bash stub that merely forked
# sleep would die while sleep kept the pipe open -- the wrapper's `$(...)` then waits for
# sleep to finish anyway, and the test measures the sleep rather than the bound.
_UV_STUB = """#!/bin/bash
case "$*" in
  *ansible-playbook*) {recap}; exit 0 ;;
  *deploy_tags.py\\ list*) {list} ;;
{locks}
  *) exit 0 ;;
esac
""".replace("{recap}", FAKE_RECAP).replace("{locks}", UV_WRAPPER_ARMS)


def _run(
    tmp_path: Path, list_stub: str, **env_overrides: str
) -> subprocess.CompletedProcess:
    """A full run (no --tags) of deploy.sh against a throwaway repo, `uv` and `flock` stubbed."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "flock").write_text(FLOCK_STUB)
    (bin_dir / "uv").write_text(_UV_STUB.replace("{list}", list_stub))
    for stub in ("flock", "uv"):
        (bin_dir / stub).chmod(0o755)
    repo = make_snapshot_repo(tmp_path / "repo")
    env = deploy_sh_env(tmp_path, bin_dir, **env_overrides)
    return subprocess.run(
        [str(_DEPLOY_SH), "--skip-tag-check", "--skip-staleness-check"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_a_slow_tag_enumeration_is_abandoned_at_the_bound(tmp_path):
    """RED half: the list hangs, the bound fires, and the run says so in its own words."""
    result = _run(tmp_path, "exec sleep 30", HOMELAB_DEPLOY_TAG_LIST_TIMEOUT="1")
    assert result.returncode == _SNAPSHOT_FAILED, result.stderr
    assert "took longer than 1s" in result.stderr
    assert "nothing was deployed" in result.stderr
    # Not the generic message: a timeout and a list that printed nothing have different fixes.
    assert "could not list the deploy tags" not in result.stderr


def test_a_fast_tag_enumeration_is_untouched_by_the_bound(tmp_path):
    """CLEAN half: a list that answers at once deploys as before."""
    result = _run(
        tmp_path,
        "printf 'alpha\\\\nbeta\\\\n'; exit 0",
        HOMELAB_DEPLOY_TAG_LIST_TIMEOUT="30",
    )
    assert result.returncode == 0, result.stderr
    assert "took longer" not in result.stderr


def _dead_snapshots(tmp_path: Path, count: int) -> Path:
    """`count` ownerless directories under the snapshot root the run will use.

    Plain directories, not worktrees: the reaper falls back to `rm -rf` when `git worktree
    remove` refuses, and the stubbed `flock` succeeds on every owner lock, so each one reads
    as dead.
    """
    root = tmp_path / "snapshots"
    root.mkdir()
    for i in range(count):
        (root / f"dead-{i:02d}").mkdir()
    return root


def test_the_reaper_stops_at_the_per_run_cap_and_says_so(tmp_path):
    """RED half: more dead snapshots than the cap, and the surplus survives this run."""
    root = _dead_snapshots(tmp_path, 5)
    result = _run(
        tmp_path, "printf 'alpha\\\\n'; exit 0", HOMELAB_DEPLOY_REAP_MAX_PER_RUN="2"
    )
    assert result.returncode == 0, result.stderr
    survivors = sorted(p.name for p in root.iterdir() if p.name.startswith("dead-"))
    assert survivors == ["dead-02", "dead-03", "dead-04"], survivors
    assert "reaped 2 dead snapshots" in result.stderr
    assert "per-run cap" in result.stderr


def test_the_reaper_clears_a_root_under_the_cap_in_silence(tmp_path):
    """CLEAN half: fewer dead snapshots than the cap, all removed, no cap line."""
    root = _dead_snapshots(tmp_path, 3)
    result = _run(
        tmp_path, "printf 'alpha\\\\n'; exit 0", HOMELAB_DEPLOY_REAP_MAX_PER_RUN="20"
    )
    assert result.returncode == 0, result.stderr
    assert not [p for p in root.iterdir() if p.name.startswith("dead-")]
    assert "per-run cap" not in result.stderr


def test_exactly_the_cap_is_not_over_it(tmp_path):
    """The line fires on a snapshot the run LEFT, not on reaching the count."""
    root = _dead_snapshots(tmp_path, 2)
    result = _run(
        tmp_path, "printf 'alpha\\\\n'; exit 0", HOMELAB_DEPLOY_REAP_MAX_PER_RUN="2"
    )
    assert result.returncode == 0, result.stderr
    assert not [p for p in root.iterdir() if p.name.startswith("dead-")]
    assert "per-run cap" not in result.stderr
