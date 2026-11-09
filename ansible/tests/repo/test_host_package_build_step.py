"""ci.yml's `host_packages_build` build step and the hand-off it uploads (#7706, #7707).

The build step's shell runs here against a scratch render and a stub `nfpm`, so each test reads
what a real run would do: both sides build concurrently, and a failing spec on either side fails
the step with both sides' logs printed.

Run: uv run pytest ansible/tests/repo/test_host_package_build_step.py
"""

from pathlib import Path

import pytest
from _helpers import REPO
from lib import yaml_fast
from lib.git_testing import git, git_out, scrubbed_env
from lib.proc_testing import fake_bin, run

CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
BUILD_STEP = "Build both renders with the pinned nfpm"
UPLOAD_STEP = "Upload the hand-off for the cases legs"
# Sorted first in each side, so a failure here is followed by a spec that succeeds.
FIRST_SPEC = "a-first"
SPECS = (FIRST_SPEC, "b-second")


def _step(name: str) -> dict:
    steps = yaml_fast.safe_load(CI_WORKFLOW.read_text())["jobs"]["host_packages_build"][
        "steps"
    ]
    (step,) = [step for step in steps if step.get("name") == name]
    return step


def _build(tmp_path: Path, broken: str | None) -> tuple[int, str]:
    """Run the build step with a stub nfpm that fails on `broken`'s first spec."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    base = git_out(repo, "rev-parse", "HEAD")
    git(repo, "commit", "-q", "--allow-empty", "-m", "head")
    temp = tmp_path / "runner"
    for side in ("base", "head"):
        for spec in SPECS:
            tree = temp / "render" / side / spec
            tree.mkdir(parents=True)
            (tree / "nfpm.yaml").write_text("name: fixture\n")
    fail = (
        f"*/render/{broken}/{FIRST_SPEC}) echo 'nfpm: broken spec' >&2; exit 3 ;;"
        if broken
        else ""
    )
    stubs = fake_bin(
        tmp_path / "bin",
        nfpm=f'case "$PWD" in {fail} esac\necho "built $PWD version $HOMELAB_VERSION tmp $TMPDIR"\n',
    )
    result = run(
        ["bash", "-c", _step(BUILD_STEP)["run"]],
        cwd=repo,
        env=scrubbed_env(RUNNER_TEMP=str(temp), BASE_SHA=base),
        stub_bin=stubs,
    )
    return result.returncode, result.stdout + result.stderr


def test_both_sides_build_with_their_own_version_and_scratch(tmp_path: Path) -> None:
    rc, log = _build(tmp_path, broken=None)
    assert rc == 0, log
    temp = tmp_path / "runner"
    for side, count in (("base", 1), ("head", 2)):
        for spec in SPECS:
            line = next(x for x in log.splitlines() if f"render/{side}/{spec} " in x)
            assert f"version 0.{count}+g" in line, line
            assert f"tmp {temp}/nfpm-tmp/{side}" in line, line


@pytest.mark.parametrize("broken", ["base", "head"])
def test_a_failing_build_on_either_side_fails_the_step(
    tmp_path: Path, broken: str
) -> None:
    rc, log = _build(tmp_path, broken=broken)
    assert rc != 0, log
    assert f"::error::the {broken} build exited" in log
    other = "head" if broken == "base" else "base"
    assert f"::error::the {other} build exited" not in log
    # The other side ran to the end and its log was printed.
    assert f"render/{other}/b-second " in log
    assert "nfpm: broken spec" in log


def test_the_hand_off_uploads_without_compressing_again() -> None:
    upload = _step(UPLOAD_STEP)
    assert upload["uses"].startswith("actions/upload-artifact@")
    assert upload["with"]["compression-level"] == 0
