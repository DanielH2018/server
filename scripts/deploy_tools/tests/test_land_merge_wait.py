#!/usr/bin/env python3
"""Tests for land.sh's --await-merge loop, run as a real process against a stubbed `gh`.

The failure this guards is a landing that SITS. A conflicting PR stays OPEN, and the loop
read only `.state`, so it could not tell "not merged yet" from "cannot be merged" and burned
the whole 2700s merge budget before printing merge-timeout. Every occurrence ended with the
operator noticing and saying so.

The accept half is the one that matters as much: GitHub computes mergeability
asynchronously and serves UNKNOWN until it settles, so a bail on anything-but-MERGEABLE
would abort every landing that polled too early while looking correct against a genuinely
conflicting PR.

These run land.sh itself rather than grepping its source -- a source assertion proves the
string exists, not that the branch fires.

Run: uv run pytest scripts/deploy_tools/tests/test_land_merge_wait.py
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_LAND_SH = Path(__file__).resolve().parent.parent / "land.sh"


def _run_await_merge(
    tmp_path: Path, states: list[str]
) -> subprocess.CompletedProcess[str]:
    """Run `land.sh --await-merge` with a `gh` that serves `states`, one per poll.

    Arguments:
      tmp_path: a scratch directory, used as both the stub's home and land.sh's checkout.
      states: what each successive `gh pr view --json state,mergeable` call prints, e.g.
        "OPEN CONFLICTING". The last entry repeats if the loop polls past the end.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (tmp_path / "states").write_text("\n".join(states) + "\n")
    stub = bin_dir / "gh"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'n=$(cat "{tmp_path}/count" 2>/dev/null || echo 1)\n'
        f'echo $((n + 1)) > "{tmp_path}/count"\n'
        f'sed -n "${{n}}p" "{tmp_path}/states" || tail -n1 "{tmp_path}/states"\n'
    )
    stub.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "LAND_MERGE_POLL": "0",
        "LAND_PRIMARY": str(tmp_path),
    }
    return subprocess.run(
        ["bash", str(_LAND_SH), "--pr", "999", "--await-merge"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.parametrize(
    "states, verdict",
    [
        (["OPEN CONFLICTING", "OPEN CONFLICTING"], "merge-conflict"),
        # The base moving under a PR flips the field for a poll while GitHub recomputes, so
        # one CONFLICTING between mergeable readings must not end the landing.
        (["OPEN CONFLICTING", "OPEN MERGEABLE", "CLOSED MERGEABLE"], None),
        # UNKNOWN is what a freshly opened PR reads (#657 served it live on 2026-09-02).
        (["OPEN UNKNOWN", "OPEN UNKNOWN", "CLOSED UNKNOWN"], None),
    ],
)
def test_only_a_settled_conflict_ends_the_wait(tmp_path, states, verdict):
    result = _run_await_merge(tmp_path, states)
    assert result.returncode == 1, result.stderr
    if verdict == "merge-conflict":
        assert "VERDICT: merge-conflict" in result.stdout
        assert "rebase" in result.stderr
    else:
        assert "merge-conflict" not in result.stdout
        assert "closed without merging" in result.stderr


def test_a_merged_pr_still_leaves_the_wait(tmp_path):
    """The loop's own accept half: MERGED must break out and reach the next phase, so the
    new branch cannot have swallowed the normal path. land.sh goes on to resolve the merge
    commit, which the stub answers with the same line -- that failure is past the wait."""
    result = _run_await_merge(tmp_path, ["MERGED MERGEABLE"])
    assert "merged after" in result.stdout
    assert "== 1/6" in result.stdout
