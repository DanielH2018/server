"""`land.sh --arm-merge` end to end: argparse -> pipeline -> the argv `gh` really receives.

Run: uv run pytest scripts/deploy_tools/tests/test_land_arm_merge_through_the_shim.py

Every other land_lib test calls a phase against a fake `Tools`, so a break in the wiring
between the command line, `pipeline._phases` and `tools.gh` fails none of them (issue #1067).
This module is the one that runs the shim as a process against stubs on PATH, the way the
deleted test_land_arm_merge.py did, and reads the recorded argv back.

The two tests are a pair: an OPEN PR must produce the merge argv, and a MERGED one must
produce none. An assertion that only ever sees the arming path cannot tell a `--arm-merge`
that always fires from one that fires correctly.

Both runs end at `nothing-to-deploy`: the stub answers an empty PR file list, which reaches
no service tag and no plane. That is far enough to prove `LAND_PRIMARY` reaches a subprocess
-- `git` runs with the primary checkout as its cwd, and the recorded cwd is asserted.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_LAND_SH = Path(__file__).resolve().parents[1] / "land.sh"
_MERGE_ARGV = "pr merge 939 --squash --auto --subject t"

_GH_STUB = """#!/bin/sh
printf '%s\\t%s\\n' "$PWD" "$*" >> "{calls}/gh-calls"
case "$*" in
  *"--json state,title"*)
    printf '{{"state":"{state}","title":"Bump vale to 3.19.0"}}\\n' ;;
  *autoMergeRequest*)
    printf '{{"state":"OPEN","mergeStateStatus":"BLOCKED","autoMergeRequest":{{"enabledAt":"2026-09-04T00:00:00Z"}}}}\\n' ;;
  *mergeCommit*)
    printf '{{"mergeCommit":{{"oid":"1f0e7c4a9b2d5e6f8a0c1b3d4e5f60718293a4b5"}}}}\\n' ;;
  *changedFiles*)
    printf '{{"files":[],"changedFiles":0}}\\n' ;;
  *)
    printf '{{}}\\n' ;;
esac
"""

# Records the cwd it was given and answers nothing, which is what `pr_range` needs to give up
# on the PR's own range rather than reach `deploy_tags`.
_GIT_STUB = """#!/bin/sh
printf '%s\\t%s\\n' "$PWD" "$*" >> "{calls}/git-calls"
"""


def _stub_bin(tmp_path: Path, state: str) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, template in (("gh", _GH_STUB), ("git", _GIT_STUB)):
        stub = bin_dir / name
        stub.write_text(template.format(calls=tmp_path, state=state))
        stub.chmod(0o755)
        (tmp_path / f"{name}-calls").touch()
    return bin_dir


def _run(tmp_path: Path, state: str) -> subprocess.CompletedProcess[str]:
    """`land.sh --pr 939 --arm-merge --subject t` against the stubs, primary = tmp_path.

    `GIT_*` is stripped from the environment: `git commit` exports `GIT_DIR` and
    `GIT_INDEX_FILE` to its hooks, and a test inheriting them has written the real repo.
    """
    bin_dir = _stub_bin(tmp_path, state)
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "LAND_PRIMARY": str(tmp_path),
    }
    return subprocess.run(
        ["bash", str(_LAND_SH), "--pr", "939", "--arm-merge", "--subject", "t"],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


def _calls(tmp_path: Path, name: str) -> list[tuple[str, str]]:
    lines = (tmp_path / f"{name}-calls").read_text().splitlines()
    return [(cwd, argv) for cwd, _, argv in (line.partition("\t") for line in lines)]


def test_an_open_pr_reaches_gh_as_the_merge_argv(tmp_path):
    result = _run(tmp_path, "OPEN")

    assert result.returncode == 0, result.stderr
    assert "== arm  arming PR #939's merge" in result.stdout
    assert "auto-merge armed: t" in result.stdout
    assert _MERGE_ARGV in [argv for _, argv in _calls(tmp_path, "gh")]
    # The arm falls through into the rest of the procedure rather than ending the landing.
    assert "== 1/6  resolving PR #939" in result.stdout
    assert "VERDICT: nothing-to-deploy" in result.stdout


def test_an_already_merged_pr_reaches_gh_with_no_merge_argv(tmp_path):
    """The rejecting half: `--arm-merge` is idempotent, so a MERGED PR is left alone."""
    result = _run(tmp_path, "MERGED")

    assert result.returncode == 0, result.stderr
    assert "already merged; --arm-merge is a no-op" in result.stdout
    assert not [
        argv for _, argv in _calls(tmp_path, "gh") if argv.startswith("pr merge")
    ]
    assert "VERDICT: nothing-to-deploy" in result.stdout


@pytest.mark.parametrize("tool", ["gh", "git"])
def test_every_subprocess_stays_out_of_the_live_checkout(tmp_path, tool):
    """`LAND_PRIMARY` decides where git runs; without it every one of these ran in
    `/home/ubuntu/server`, the deploy host's own checkout (issue #1067)."""
    _run(tmp_path, "OPEN")

    calls = _calls(tmp_path, tool)
    assert calls, (
        f"the {tool} stub recorded nothing, so this proves nothing about where it ran"
    )
    if tool == "git":
        assert {cwd for cwd, _ in calls} == {str(tmp_path)}
    assert "/home/ubuntu/server" not in {cwd for cwd, _ in calls}
