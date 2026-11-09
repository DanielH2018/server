"""ci.yml's `host_packages_build` build step and the hand-off it uploads (#7706, #7707, #7834).

The build step's shell runs here against a scratch render and a stub `nfpm`, so each test reads
what a real run would do: every package of both sides packs at once, and a failing spec on
either side fails the step with every log printed.

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


def _build(
    tmp_path: Path, broken: str | None, before_build: str = ""
) -> tuple[int, str]:
    """Run the build step with a stub nfpm that fails on `broken`'s first spec.

    `before_build` is shell the stub runs ahead of everything else.
    """
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
        nfpm=f'{before_build}\ncase "$PWD" in {fail} esac\necho "built $PWD version $HOMELAB_VERSION tmp $TMPDIR"\n',
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
    assert f"::error::the {broken} build of {FIRST_SPEC} exited 3" in log
    other = "head" if broken == "base" else "base"
    assert f"::error::the {other} build" not in log
    assert f"::error::the {broken} build of b-second" not in log
    # Every other pack ran to the end and its log was printed.
    assert f"render/{broken}/b-second " in log
    assert f"render/{other}/b-second " in log
    assert "nfpm: broken spec" in log


def test_every_pack_runs_at_once(tmp_path: Path) -> None:
    """Each stub pack waits until all four have started, so a serial pack never finishes."""
    arrived = tmp_path / "arrived"
    arrived.mkdir()
    barrier = (
        f'touch "{arrived}/$$"\n'
        "for _ in $(seq 100); do\n"
        f'  [ "$(ls "{arrived}" | wc -l)" -ge 4 ] && break\n'
        "  sleep 0.05\n"
        "done\n"
        f'[ "$(ls "{arrived}" | wc -l)" -ge 4 ] || {{ echo "nfpm: packed alone" >&2; exit 9; }}'
    )
    rc, log = _build(tmp_path, broken=None, before_build=barrier)
    assert rc == 0, log


HANDOFF_STEP = "Hand the render, the debs and nfpm to the cases legs"
# What `host_package_cases.Cases` and its case modules read from the hand-off.
CASES_READ = {
    "render/head/packages/homelab-updater/nfpm.yaml",
    "render/head/hosts/daniel-pi/nfpm.yaml",
    "debs/base/homelab-updater_0.1_all.deb",
    "debs/head/homelab-updater_0.2_all.deb",
    "debs/head/homelab-daniel-pi_0.2_all.deb",
    "debs/head/homelab-tools_0.2_amd64.deb",
    "bin/nfpm",
}
NOBODY_READS = {
    "render/base/packages/homelab-tools/amd64/root/usr/lib/homelab/bin/flux",
    "render/head/packages/homelab-tools/arm64/root/usr/lib/homelab/bin/flux",
    "debs/base/homelab-tools_0.1_amd64.deb",
    "debs/base/homelab-tools_0.1_arm64.deb",
    "debs/head/homelab-tools_0.2_arm64.deb",
}


def test_the_hand_off_carries_what_the_cases_read_and_no_tool_binaries(
    tmp_path: Path,
) -> None:
    temp = tmp_path / "runner"
    for name in CASES_READ | NOBODY_READS:
        (temp / name).parent.mkdir(parents=True, exist_ok=True)
        (temp / name).write_text(name)
    result = run(
        ["bash", "-c", _step(HANDOFF_STEP)["run"]],
        cwd=tmp_path,
        env=scrubbed_env(RUNNER_TEMP=str(temp)),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    listed = run(["tar", "-tf", str(temp / "host-packages-build.tar")], cwd=tmp_path)
    members = {line for line in listed.stdout.splitlines() if not line.endswith("/")}
    assert members == CASES_READ


def test_the_hand_off_uploads_without_compressing_again() -> None:
    upload = _step(UPLOAD_STEP)
    assert upload["uses"].startswith("actions/upload-artifact@")
    assert upload["with"]["compression-level"] == 0
