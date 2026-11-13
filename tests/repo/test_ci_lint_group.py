"""The lint tools sit in their own uv group, and a CI job that runs no lint hook skips it.

actionlint-py ships no wheel, so installing it downloads the actionlint binary from a GitHub
release. A release outage (#7625) then failed every job that installed it, the six pytest
shards included, though no test runs it. pyproject.toml's `lint` group holds the tools only
prek hooks call, `[tool.uv] default-groups` keeps a local `uv run` installing them, and each
job in any workflow that runs no prek hook sets `UV_NO_GROUP: lint`, at job or workflow level.

Run: uv run pytest tests/repo/test_ci_lint_group.py
"""

import copy
import re
import tomllib

import pytest
from _helpers import REPO
from lib import yaml_fast

PYPROJECT = REPO / "pyproject.toml"
WORKFLOWS = REPO / ".github" / "workflows"
LINT_TOOLS = {"actionlint-py", "zizmor", "ty", "mkdocs-material", "ruff"}
# (workflow, job) pairs each side of the split must contain, so a reshaped workflow cannot pass
# empty. promote.yml's render sets the variable at workflow level.
KNOWN_LINT_FREE = {
    ("ci.yml", "pytest"),
    ("ci.yml", "pr_gates"),
    ("ci.yml", "flux_roundtrip"),
    ("ci.yml", "sops_manifest_type_proof"),
    ("promote.yml", "render"),
    ("render-diff.yml", "render-diff"),
    ("image-leg.yml", "image-leg"),
    ("bootstrap-drill.yml", "drill"),
    ("flux-deletion-drill.yml", "drill"),
    ("prune-artifacts.yml", "prune"),
}
KNOWN_LINTING = {("ci.yml", "hooks")}
RUNS_A_HOOK = re.compile(
    r"\bprek run\b|\buv run (?:--\S+ )*(?:ruff|ty|zizmor|actionlint|mkdocs)\b"
)


def _name(requirement: str) -> str:
    return re.split(r"[<>=!~;\[ ]", requirement, maxsplit=1)[0]


def pyproject_problems(config: dict) -> list[str]:
    groups = config.get("dependency-groups", {})
    found = []
    lint = {_name(r) for r in groups.get("lint", [])}
    if not LINT_TOOLS <= lint:
        found.append(f"the lint group lacks {sorted(LINT_TOOLS - lint)}")
    for group, requirements in groups.items():
        stray = (
            LINT_TOOLS & {_name(r) for r in requirements} if group != "lint" else set()
        )
        if stray:
            found.append(f"the {group} group also lists {sorted(stray)}")
    defaults = config.get("tool", {}).get("uv", {}).get("default-groups", [])
    if not {"dev", "lint"} <= set(defaults):
        found.append("[tool.uv] default-groups does not name both dev and lint")
    return found


def _uses_uv(job: dict) -> bool:
    for step in job.get("steps") or []:
        if str(step.get("uses", "")) == "$/.github/actions/setup":
            return (step.get("with") or {}).get("uv", "true") != "false"
    return False


def _runs_a_hook(job: dict) -> bool:
    for step in job.get("steps") or []:
        if RUNS_A_HOOK.search(str(step.get("run", ""))):
            return True
        if (step.get("with") or {}).get("prek-cache-key"):
            return True
    return False


def split(workflow: dict) -> tuple[set[str], set[str]]:
    """The uv jobs that run a lint hook, and those that run none."""
    jobs = {n: j for n, j in workflow["jobs"].items() if _uses_uv(j)}
    linting = {n for n, j in jobs.items() if _runs_a_hook(j)}
    return linting, set(jobs) - linting


def workflow_problems(workflow: dict) -> list[str]:
    linting, lint_free = split(workflow)
    top = workflow.get("env") or {}
    found = []
    for name in sorted(lint_free):
        env = {**top, **(workflow["jobs"][name].get("env") or {})}
        if env.get("UV_NO_GROUP") != "lint":
            found.append(
                f"{name}: runs no lint hook but does not set UV_NO_GROUP: lint"
            )
    for name in sorted(linting):
        job = workflow["jobs"][name]
        scopes = [workflow, job, *(job.get("steps") or [])]
        if any("UV_NO_GROUP" in (s.get("env") or {}) for s in scopes):
            found.append(f"{name}: runs a lint hook but sets UV_NO_GROUP")
    return found


@pytest.fixture(scope="module")
def config() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _load(name: str) -> dict:
    return yaml_fast.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


ALL_WORKFLOWS = sorted(p.name for p in WORKFLOWS.glob("*.yml"))


@pytest.fixture(scope="module")
def workflow() -> dict:
    return _load("ci.yml")


def test_the_lint_tools_sit_only_in_the_lint_group(config):
    assert pyproject_problems(config) == []


def test_a_lint_tool_back_in_dev_is_flagged(config):
    broken = copy.deepcopy(config)
    broken["dependency-groups"]["dev"].append("actionlint-py")
    assert "the dev group also lists ['actionlint-py']" in pyproject_problems(broken)


def test_a_lint_group_no_uv_run_installs_is_flagged(config):
    broken = copy.deepcopy(config)
    broken["tool"]["uv"]["default-groups"] = ["dev"]
    assert any("default-groups" in p for p in pyproject_problems(broken))


def test_the_split_finds_the_known_jobs():
    linting, lint_free = set(), set()
    for name in ALL_WORKFLOWS:
        hooked, free = split(_load(name))
        linting |= {(name, job) for job in hooked}
        lint_free |= {(name, job) for job in free}
    assert KNOWN_LINT_FREE <= lint_free, sorted(lint_free)
    assert KNOWN_LINTING <= linting, sorted(linting)


@pytest.mark.parametrize("name", ALL_WORKFLOWS)
def test_every_lint_free_job_skips_the_lint_group(name):
    assert workflow_problems(_load(name)) == []


def test_a_lint_free_job_outside_ci_installing_the_group_is_flagged():
    broken = _load("render-diff.yml")
    del broken["jobs"]["render-diff"]["env"]["UV_NO_GROUP"]
    assert workflow_problems(broken) == [
        "render-diff: runs no lint hook but does not set UV_NO_GROUP: lint"
    ]


def test_a_lint_free_job_installing_the_group_is_flagged(workflow):
    broken = copy.deepcopy(workflow)
    del broken["jobs"]["pytest"]["env"]["UV_NO_GROUP"]
    assert workflow_problems(broken) == [
        "pytest: runs no lint hook but does not set UV_NO_GROUP: lint"
    ]


def test_a_linting_job_skipping_the_group_is_flagged(workflow):
    broken = copy.deepcopy(workflow)
    broken["jobs"]["hooks"].setdefault("env", {})["UV_NO_GROUP"] = "lint"
    assert workflow_problems(broken) == ["hooks: runs a lint hook but sets UV_NO_GROUP"]
