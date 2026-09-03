#!/usr/bin/env python3
"""Tests for land.sh's --arm-merge flag.

The unattended renovate-agent run cannot answer a permission prompt, and `gh pr merge` sits
on the ask list -- auto mode suspends the allow list, so the classifier judges the command
text itself and asks. Three attempts, three denials, on 2026-09-03 (issue #979). --arm-merge
runs `gh pr merge --squash --auto` inside land.sh instead, so the merge happens behind the
same single script invocation the worktree-containment check already accepts.

These run land.sh itself against a stubbed `gh` rather than grepping its source, the same
style as test_land_merge_wait.py -- a source assertion proves the string exists, not that the
branch fires.

Run: uv run pytest scripts/deploy_tools/tests/test_land_arm_merge.py
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_LAND_SH = Path(__file__).resolve().parent.parent / "land.sh"


def _stub_gh(tmp_path: Path, pr_state: str, title: str) -> Path:
    """Write a `gh` stub that answers `pr view` and logs every call it sees.

    `pr view --json state,title` (the arm step) and `pr view --json mergeCommit` (the
    resolve step land.sh reaches right after arming) are told apart by which field name is
    on the command line, since $* is all a stub this simple gets to key on.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$*" >> "{tmp_path}/calls"\n'
        'case "$*" in\n'
        '  *"state,title"*)\n'
        f'    printf "{pr_state}\\t{title}\\n"\n'
        "    ;;\n"
        "  *mergeCommit*)\n"
        '    echo ""\n'
        "    ;;\n"
        "esac\n"
    )
    gh.chmod(0o755)
    return gh


def _run_arm_merge(
    tmp_path: Path, pr_state: str = "OPEN", title: str = "Bump vale to 3.19.0"
):
    _stub_gh(tmp_path, pr_state, title)
    env = {
        **os.environ,
        "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
        "LAND_PRIMARY": str(tmp_path),
    }
    return subprocess.run(
        ["bash", str(_LAND_SH), "--pr", "939", "--arm-merge"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_arm_merge_calls_gh_pr_merge_with_the_pr_title(tmp_path):
    result = _run_arm_merge(tmp_path)
    assert "== arm  arming PR #939's merge" in result.stdout
    assert "auto-merge armed: Bump vale to 3.19.0" in result.stdout
    calls = (tmp_path / "calls").read_text()
    assert "pr merge 939 --squash --auto --subject Bump vale to 3.19.0" in calls
    # land.sh reaches its next step (resolving the merge commit) rather than stopping at the
    # arm step -- --arm-merge falls through into the rest of the procedure by construction.
    assert "== 1/6  resolving PR #939" in result.stdout


def test_arm_merge_subject_overrides_the_pr_title(tmp_path):
    _stub_gh(tmp_path, "OPEN", "Bump vale to 3.19.0")
    env = {
        **os.environ,
        "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
        "LAND_PRIMARY": str(tmp_path),
    }
    subprocess.run(
        [
            "bash",
            str(_LAND_SH),
            "--pr",
            "939",
            "--arm-merge",
            "--subject",
            "Bump vale, take 2",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    calls = (tmp_path / "calls").read_text()
    assert "pr merge 939 --squash --auto --subject Bump vale, take 2" in calls


def test_arm_merge_is_a_no_op_on_an_already_merged_pr(tmp_path):
    """Idempotent: a retried invocation (the retry the merge-wait timeout tells a session to
    make) must not re-arm a PR that already merged."""
    result = _run_arm_merge(tmp_path, pr_state="MERGED", title="Bump vale to 3.19.0")
    assert "already merged; --arm-merge is a no-op" in result.stdout
    calls = (tmp_path / "calls").read_text()
    assert "pr merge 939" not in calls


def test_arm_merge_requires_pr(tmp_path):
    env = {**os.environ, "LAND_PRIMARY": str(tmp_path)}
    result = subprocess.run(
        ["bash", str(_LAND_SH), "--arm-merge"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2
    assert "--pr is required" in result.stderr


@pytest.mark.parametrize("flag", ["--arm-merge", "--subject"])
def test_subject_alone_still_requires_pr(tmp_path, flag):
    env = {**os.environ, "LAND_PRIMARY": str(tmp_path)}
    args = ["bash", str(_LAND_SH), flag]
    if flag == "--subject":
        args.append("x")
    result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 2
    assert "--pr is required" in result.stderr
