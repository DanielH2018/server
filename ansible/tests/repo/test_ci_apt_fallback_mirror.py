"""Every apt install on a CI runner happens after the setup action adds its fallback mirror.

The runner's apt sources list only ubuntu.com hosts, so a DNS outage there failed all six pytest
shards at their `age` install (#7957). `.github/actions/setup` appends a kernel.org mirror as
its first step, which helps only a step that runs after it.

Run: uv run pytest ansible/tests/repo/test_ci_apt_fallback_mirror.py
"""

from _helpers import REPO
from lib import yaml_fast

SETUP = "$/.github/actions/setup"
ACTION = REPO / ".github" / "actions" / "setup" / "action.yml"
WORKFLOWS = REPO / ".github" / "workflows"
# (workflow, job) pairs the census must find, so a reshaped workflow cannot pass empty.
KNOWN_APT_JOBS = {("ci.yml", "pytest"), ("bootstrap-drill.yml", "drill")}


def apt_steps_before_setup(job: dict) -> list[str]:
    """The names of a job's runner-side apt steps that no earlier step gives the fallback."""
    found = []
    for step in job.get("steps") or []:
        if str(step.get("uses", "")) == SETUP:
            return found
        run = str(step.get("run", ""))
        # apt inside `docker run` reads the container's sources, which the runner's list never reaches.
        if "apt-get" in run and "docker run" not in run:
            found.append(str(step.get("name", run.splitlines()[0])))
    return found


def _apt_jobs() -> dict[tuple[str, str], dict]:
    jobs = {}
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for name, job in (
            yaml_fast.safe_load(path.read_text()).get("jobs") or {}
        ).items():
            runs = [str(s.get("run", "")) for s in job.get("steps") or []]
            if any("apt-get" in r and "docker run" not in r for r in runs):
                jobs[(path.name, name)] = job
    return jobs


def test_the_fallback_mirror_is_the_setup_actions_first_step():
    first = yaml_fast.safe_load(ACTION.read_text())["runs"]["steps"][0]
    assert "/etc/apt/apt-mirrors.txt" in first["run"]
    assert "https://mirrors.edge.kernel.org/ubuntu/" in first["run"]
    assert "if" not in first, "the fallback must apply whichever inputs a job passes"


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
