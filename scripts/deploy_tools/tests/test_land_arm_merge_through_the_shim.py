"""`land.sh --arm-merge` end to end: argparse -> pipeline -> the argv `gh` really receives.

Run: uv run pytest scripts/deploy_tools/tests/test_land_arm_merge_through_the_shim.py

Every other land_lib test calls a phase against a fake `Tools`, so a break in the wiring
between the command line, `pipeline._phases` and `tools.gh` fails none of them.
This module is the one that runs the shim as a process against stubs on PATH, the way the
deleted test_land_arm_merge.py did, and reads the recorded argv back.

The two tests are a pair: an OPEN PR must be handed to the merge wait, and a MERGED one must
not be. An assertion that only ever sees the arming path cannot tell a `--arm-merge` that
always fires from one that fires correctly. Neither run issues a `gh pr merge`: every PR
merges through await_merge's REST merge (#4001). That merge needs await_ci to read the head
green over HTTP, which the stubs cannot answer, so the stubbed poll reads the PR MERGED and
`test_land_merge_past_review.py` pins the REST argv instead.

Both runs end at `nothing-to-deploy`: the stub answers an empty PR file list, which reaches
no service tag and no plane. That is far enough to prove `LAND_PRIMARY` reaches a subprocess
-- `git` runs with the primary checkout as its cwd, and the recorded cwd is asserted.
"""

import subprocess
from pathlib import Path
from lib.git_testing import scrubbed_env
from lib.proc_testing import fake_bin, path_with


_LAND_SH = Path(__file__).resolve().parents[1] / "land.sh"
_POLL_ARGV = "pr view 939 --json state,mergeable,headRefOid"

_GH_STUB = """#!/bin/sh
printf '%s\\t%s\\n' "$PWD" "$*" >> "{calls}/gh-calls"
case "$*" in
  *"--json state,title"*)
    printf '{{"state":"{state}","title":"Bump vale to 3.19.0"}}\\n' ;;
  *"--json state,mergeable,headRefOid"*)
    printf '{{"state":"MERGED","mergeable":"MERGEABLE","headRefOid":""}}\\n' ;;
  "api repos/{{owner}}/{{repo}}")
    printf '{{"visibility":"public"}}\\n' ;;
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
    for name in ("gh", "git"):
        (tmp_path / f"{name}-calls").touch()
    return fake_bin(
        tmp_path / "bin",
        gh=_GH_STUB.format(calls=tmp_path, state=state),
        git=_GIT_STUB.format(calls=tmp_path, state=state),
    )


def _run(tmp_path: Path, state: str) -> subprocess.CompletedProcess[str]:
    """`land.sh --pr 939 --arm-merge --await-merge --subject t` against the stubs.

    The primary checkout is tmp_path.

    `GIT_*` is stripped from the environment: `git commit` exports `GIT_DIR` and
    `GIT_INDEX_FILE` to its hooks, and a test inheriting them has written the real repo.
    """
    bin_dir = _stub_bin(tmp_path, state)
    env = {
        **scrubbed_env(),
        "PATH": path_with(bin_dir),
        "LAND_PRIMARY": str(tmp_path),
    }
    return subprocess.run(
        [
            "bash",
            str(_LAND_SH),
            "--pr",
            "939",
            "--arm-merge",
            "--await-merge",
            "--subject",
            "t",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


def _calls(tmp_path: Path, name: str) -> list[tuple[str, str]]:
    lines = (tmp_path / f"{name}-calls").read_text().splitlines()
    return [(cwd, argv) for cwd, _, argv in (line.partition("\t") for line in lines)]


def test_an_open_pr_is_handed_to_the_merge_wait(tmp_path):
    result = _run(tmp_path, "OPEN")

    assert result.returncode == 0, result.stderr
    assert "== arm  arming PR #939's merge" in result.stdout
    assert "merging directly once CI is green" in result.stdout
    gh = [argv for _, argv in _calls(tmp_path, "gh")]
    assert any(argv.startswith(_POLL_ARGV) for argv in gh), gh
    assert not [argv for argv in gh if argv.startswith("pr merge")]
    # The arm falls through into the rest of the procedure rather than ending the landing.
    assert "== 1/6  resolving PR #939" in result.stdout
    assert "VERDICT: nothing-to-deploy" in result.stdout


def test_an_already_merged_pr_is_not_handed_to_the_merge_wait(tmp_path):
    """The rejecting half: `--arm-merge` is idempotent, so a MERGED PR is left alone."""
    result = _run(tmp_path, "MERGED")

    assert result.returncode == 0, result.stderr
    assert "already merged; --arm-merge is a no-op" in result.stdout
    assert "merging directly once CI is green" not in result.stdout
    assert not [
        argv for _, argv in _calls(tmp_path, "gh") if argv.startswith("pr merge")
    ]
    assert "VERDICT: nothing-to-deploy" in result.stdout


def test_git_runs_in_land_primary_rather_than_the_live_checkout(tmp_path):
    """`LAND_PRIMARY` decides which checkout the landing's git calls read.

    Until `parse_args` read it, `Options.primary` was `/home/ubuntu/server` — a real
    directory on the deploy host, so `_phases` passed its `is_dir` check and `fetch_branch`
    ran there. This is the assertion that would have caught it.
    """
    _run(tmp_path, "OPEN")

    git_calls = _calls(tmp_path, "git")
    assert git_calls, (
        "the git stub recorded nothing, so this proves nothing about where it ran"
    )
    assert {cwd for cwd, _ in git_calls} == {str(tmp_path)}
