"""Every apt install on a CI runner happens after the setup action adds its fallback mirror.

The runner's apt sources list only ubuntu.com hosts, so a DNS outage there failed all six pytest
shards at their `age` install. `.github/actions/setup` appends a kernel.org mirror as
its first step, which helps only a step that runs after it.

Run: uv run pytest tests/repo/ci_workflows/test_ci_apt_fallback_mirror.py
"""

import os
import shutil

from _helpers import REPO
from lib import yaml_fast
from lib.proc_testing import fake_bin, run

SETUP = "$/.github/actions/setup"
ACTION = REPO / ".github" / "actions" / "setup" / "action.yml"
WORKFLOWS = REPO / ".github" / "workflows"
# (workflow, job) pairs the census must find, so a reshaped workflow cannot pass empty.
KNOWN_APT_JOBS = {
    ("ci.yml", "pytest"),
    ("pytest-sweep.yml", "sweep"),
    ("bootstrap-drill.yml", "drill"),
}
# The composite action that installs age and apt-utils for the pytest legs and the sweep.
PYTEST_TOOLS = "$/.github/actions/pytest-tools"
PYTEST_TOOLS_ACTION = REPO / ".github" / "actions" / "pytest-tools" / "action.yml"


def _runs_apt(step: dict) -> bool:
    """Whether the step installs from the runner's apt sources."""
    if str(step.get("uses", "")) == PYTEST_TOOLS:
        return True
    run = str(step.get("run", ""))
    # apt inside `docker run` reads the container's sources, which the runner's list never reaches.
    return "apt-get" in run and "docker run" not in run


def apt_steps_before_setup(job: dict) -> list[str]:
    """The names of a job's runner-side apt steps that no earlier step gives the fallback."""
    found = []
    for step in job.get("steps") or []:
        if str(step.get("uses", "")) == SETUP:
            return found
        if _runs_apt(step):
            found.append(
                str(step.get("name") or step.get("uses") or step["run"].splitlines()[0])
            )
    return found


def _apt_jobs() -> dict[tuple[str, str], dict]:
    jobs = {}
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for name, job in (
            yaml_fast.safe_load(path.read_text()).get("jobs") or {}
        ).items():
            if any(_runs_apt(s) for s in job.get("steps") or []):
                jobs[(path.name, name)] = job
    return jobs


def test_the_pytest_tools_action_is_an_apt_step():
    steps = yaml_fast.safe_load(PYTEST_TOOLS_ACTION.read_text())["runs"]["steps"]
    assert any("apt-get" in str(s.get("run", "")) for s in steps)


def _mirror_step() -> dict:
    return yaml_fast.safe_load(ACTION.read_text())["runs"]["steps"][0]


def test_the_fallback_mirror_is_the_setup_actions_first_step():
    first = _mirror_step()
    assert first["env"]["MIRRORS"] == "/etc/apt/apt-mirrors.txt"
    assert "https://mirrors.edge.kernel.org/ubuntu/" in first["run"]
    assert "if" not in first, "the fallback must apply whichever inputs a job passes"


def _run_mirror_step(tmp_path, sudo: str):
    """The mirror step over a scratch mirror list, with `sudo` as the fake sudo's body."""
    bash = shutil.which("bash")
    assert bash is not None
    mirrors = tmp_path / "apt-mirrors.txt"
    mirrors.write_text("http://archive.ubuntu.com/ubuntu/\n")
    done = run(
        [bash, "-eo", "pipefail", "-c", _mirror_step()["run"]],
        env={**os.environ, "MIRRORS": str(mirrors)},
        stub_bin=fake_bin(tmp_path / "bin", sudo=sudo),
    )
    return done, mirrors.read_text()


def test_the_mirror_step_with_sudo_appends_the_fallback_is_clean(tmp_path):
    done, mirrors = _run_mirror_step(tmp_path, 'shift\nexec "$@"\n')
    assert done.returncode == 0, done.stderr
    assert mirrors.splitlines()[-1] == "https://mirrors.edge.kernel.org/ubuntu/"


def test_the_mirror_step_where_sudo_is_refused_is_flagged_with_a_notice(tmp_path):
    done, mirrors = _run_mirror_step(tmp_path, "exit 1\n")
    assert done.returncode == 0, done.stderr
    assert mirrors == "http://archive.ubuntu.com/ubuntu/\n"
    assert "::notice::no sudo here" in done.stdout


def test_every_runner_apt_install_follows_the_setup_action():
    jobs = _apt_jobs()
    assert KNOWN_APT_JOBS <= set(jobs), f"census lost {KNOWN_APT_JOBS - set(jobs)}"
    problems = {
        key: steps
        for key, job in jobs.items()
        if (steps := apt_steps_before_setup(job))
    }
    assert not problems, f"apt runs before {SETUP} adds the fallback mirror: {problems}"


def test_apt_after_setup_is_clean():
    job = {
        "steps": [
            {"uses": SETUP},
            {"name": "age", "run": "sudo apt-get install -y age"},
        ]
    }
    assert apt_steps_before_setup(job) == []


def test_apt_before_setup_is_flagged():
    job = {
        "steps": [
            {"name": "age", "run": "sudo apt-get install -y age"},
            {"uses": SETUP},
        ]
    }
    assert apt_steps_before_setup(job) == ["age"]
