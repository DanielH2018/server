#!/usr/bin/env python3
"""Tests for land.sh's --await-merge loop, run as a real process against stubbed tools.

The failure this guards is a landing that SITS. An armed auto-merge never fires from two
states -- the PR conflicts with master, or the PR's own CI is red -- and GitHub reports
neither in the field the loop read. It saw only `state: OPEN`, the same reading a PR gives
while it is about to merge, so it burned the whole 2700s merge budget and printed
merge-timeout. Every occurrence ended with the operator noticing and saying so.

The accept halves matter as much as the bails, and each has its own asynchronous field to
tolerate: GitHub serves `mergeable: UNKNOWN` until it computes mergeability, and await_ci.py
answers `pending` until a required check registers. A bail on either would abort every
landing that polled too early while looking correct against a genuinely broken PR.

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
    tmp_path: Path, states: list[str], ci_rc: int = 0, ci_line: str = "dead: CI green"
) -> subprocess.CompletedProcess[str]:
    """Run `land.sh --await-merge` with a stubbed `gh` and a stubbed await_ci.py.

    Arguments:
      tmp_path: a scratch directory, used as both the stubs' home and land.sh's checkout.
      states: what each successive `gh pr view` call prints -- state, mergeable and head
        SHA, e.g. "OPEN CONFLICTING dead". The last entry repeats if the loop polls past
        the end.
      ci_rc: the exit code the stubbed await_ci.py returns; 0 green, 1 red, 75 pending.
      ci_line: what it prints, which land.sh quotes into its pr-ci-red message.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (tmp_path / "states").write_text("\n".join(states) + "\n")
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        f'n=$(cat "{tmp_path}/count" 2>/dev/null || echo 1)\n'
        f'echo $((n + 1)) > "{tmp_path}/count"\n'
        f'sed -n "${{n}}p" "{tmp_path}/states" || tail -n1 "{tmp_path}/states"\n'
    )
    gh.chmod(0o755)
    # land.sh reaches await_ci.py through `uv run python ...`, so stubbing uv serves the CI
    # verdict without a second env knob and without that script path existing here.
    uv = bin_dir / "uv"
    uv.write_text(f"#!/usr/bin/env bash\necho '{ci_line}'\nexit {ci_rc}\n")
    uv.chmod(0o755)
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
        (["OPEN CONFLICTING dead", "OPEN CONFLICTING dead"], "merge-conflict"),
        # The base moving under a PR flips the field for a poll while GitHub recomputes, so
        # one CONFLICTING between mergeable readings must not end the landing.
        (
            ["OPEN CONFLICTING dead", "OPEN MERGEABLE dead", "CLOSED MERGEABLE dead"],
            None,
        ),
        # UNKNOWN is what a freshly opened PR reads (#657 served it live on 2026-09-02).
        (["OPEN UNKNOWN dead", "OPEN UNKNOWN dead", "CLOSED UNKNOWN dead"], None),
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


def test_a_red_pr_ci_ends_the_wait(tmp_path):
    """The reject half for #814. An armed auto-merge never fires on a red required check,
    and GitHub says only `mergeStateStatus: BLOCKED` -- the same word it uses while the
    checks are still running. The verdict must name the CI, and quote what await_ci.py
    said, so the reader knows to push a fix rather than to look at the queue."""
    result = _run_await_merge(
        tmp_path, ["OPEN MERGEABLE dead"], ci_rc=1, ci_line="dead1234: CI RED"
    )
    assert result.returncode == 1, result.stdout
    assert "VERDICT: pr-ci-red" in result.stdout
    assert "dead1234: CI RED" in result.stdout


@pytest.mark.parametrize("ci_rc", [75, 2])
def test_a_ci_answer_that_is_not_red_keeps_the_wait_going(tmp_path, ci_rc):
    """The accept half, and the one that guards the landing. await_ci.py answers `pending`
    (75) until a required run registers, which IS the grace period -- a bail on anything
    but green would end every landing that polled before CI started. 2 is the disarmed
    gate, which checked nothing and so cannot condemn the PR either."""
    result = _run_await_merge(
        tmp_path, ["OPEN MERGEABLE dead", "CLOSED MERGEABLE dead"], ci_rc=ci_rc
    )
    assert "pr-ci-red" not in result.stdout
    assert "closed without merging" in result.stderr


def test_a_merged_pr_still_leaves_the_wait(tmp_path):
    """The loop's own accept half: MERGED must break out and reach the next phase, so
    neither bail can have swallowed the normal path. land.sh goes on to resolve the merge
    commit, which the stub answers with the same line -- that failure is past the wait."""
    result = _run_await_merge(tmp_path, ["MERGED MERGEABLE dead"])
    assert "merged after" in result.stdout
    assert "== 1/6" in result.stdout
