"""Every third-party module a `UV_NO_DEV` job imports installs without the dependency groups.

`promote.yml` and ci.yml's validator jobs set `UV_NO_DEV` and `UV_NO_GROUP=lint`, so they get
only pyproject.toml's `[project] dependencies` and what those require. PR CI runs the promote
scripts with the dev group installed, so a dev-only import there passes every job and then fails
every promotion on master. This test traces each script such a job runs with `modulefinder`,
which reads imports statically (function-level ones included) and resolves first-party modules
from `pythonpath`. An import it cannot resolve there is third-party, and its distribution must
be in that closure.

Run: uv run pytest tests/repo/test_promote_runtime_imports.py
"""

import importlib.metadata
import modulefinder
import re
import sys
import sysconfig
import tomllib
from pathlib import Path

import pytest
from _helpers import REPO
from test_promote_workflow import WORKFLOW, _load, _steps

PYPROJECT = REPO / "pyproject.toml"
CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
# The scripts that must appear among those the UV_NO_DEV jobs run, so a reshaped regex or a
# dropped UV_NO_DEV cannot pass empty.
KNOWN_ENTRIES = {
    "scripts/deploy_tools/promote.py",
    "scripts/renderer/homelab_render.py",
    "scripts/secrets_mgmt/cluster_vars.py",
    "scripts/validate/render_diff_gates.py",
    "scripts/validate/cluster_pins.py",
    "scripts/validate/k3s_binary_sum.py",
    "scripts/renderer/shadow_diff.py",
    "scripts/validate/host_packages.py",
    "scripts/secrets_mgmt/tests/_sops_type_proof.py",
}
# ci.yml jobs that must set UV_NO_DEV, because their scripts need only [project] dependencies.
KNOWN_NO_DEV_JOBS = {
    "pr_gates",
    "flux_roundtrip",
    "host_packages_build",
    "sops_manifest_type_proof",
}
# Scripts an entry starts as a child `python` process, which the import trace cannot follow.
CHILD_ENTRIES = {
    "scripts/validate/host_packages.py": (
        "scripts/renderer/homelab_render.py",
        "scripts/validate/validate_lib/host_fixtures.py",
    ),
    "scripts/validate/render_diff_gates.py": ("scripts/renderer/homelab_render.py",),
}
SCRIPT = re.compile(r"\bpython\s+(?:\\\s+)?(scripts/[\w/]+\.py)\b")


def _normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement_name(requirement: str) -> str:
    return _normalise(re.split(r"[<>=!~;\[ ]", requirement, maxsplit=1)[0])


def no_dev_jobs(workflow: dict) -> dict[str, dict]:
    """The jobs of `workflow` that set UV_NO_DEV, at workflow or job level."""
    top = (workflow.get("env") or {}).get("UV_NO_DEV")
    return {
        name: job
        for name, job in workflow["jobs"].items()
        if (job.get("env") or {}).get("UV_NO_DEV", top) == "1"
    }


def no_dev_entries() -> set[str]:
    """Every repo script a `run:` step of a UV_NO_DEV job invokes through `python`."""
    found = set()
    for path in (WORKFLOW, CI_WORKFLOW):
        for job in no_dev_jobs(_load(path)).values():
            for step in _steps(job):
                found.update(SCRIPT.findall(str(step.get("run", ""))))
    for entry in list(found):
        found.update(CHILD_ENTRIES.get(entry, ()))
    return found


def allowed_distributions() -> set[str]:
    """`[project] dependencies` and everything they require, as installed here."""
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    pending = [_requirement_name(dep) for dep in project.get("dependencies", [])]
    seen = set()
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        for req in importlib.metadata.requires(name) or []:
            if "extra ==" not in req:
                pending.append(_requirement_name(req))
    return seen


def _search_path(entries: list[Path]) -> list[str]:
    toml = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    roots = [REPO / p for p in toml["tool"]["pytest"]["ini_options"]["pythonpath"]]
    first_party = [str(p) for p in roots if "tests" not in p.parts]
    paths = sysconfig.get_paths()
    stdlib = [
        paths["stdlib"],
        paths["platstdlib"],
        *(p for p in sys.path if "lib-dynload" in p),
    ]
    return [*{str(e.parent) for e in entries}, *first_party, *stdlib]


def third_party_imports(entries: list[Path]) -> dict[str, set[str]]:
    """Map each distribution the entries' import closure needs to the modules that pull it in."""
    finder = modulefinder.ModuleFinder(path=_search_path(entries))
    for entry in entries:
        finder.run_script(str(entry))
    owners = importlib.metadata.packages_distributions()
    needed: dict[str, set[str]] = {}
    for module, importers in finder.badmodules.items():
        top = module.split(".")[0]
        for dist in owners.get(top, []):
            needed.setdefault(_normalise(dist), set()).update(importers)
    return needed


def problems(entries: list[Path], allowed: set[str]) -> list[str]:
    return [
        f"{dist} is imported by {sorted(importers)} but is not in [project] dependencies"
        for dist, importers in sorted(third_party_imports(entries).items())
        if dist not in allowed
    ]


@pytest.fixture(scope="module")
def entries() -> set[str]:
    return no_dev_entries()


def test_the_census_finds_the_known_scripts(entries):
    assert KNOWN_ENTRIES <= entries, (
        f"no UV_NO_DEV job runs {sorted(KNOWN_ENTRIES - entries)}"
    )


def test_the_validator_jobs_skip_the_dev_group():
    jobs = set(no_dev_jobs(_load(CI_WORKFLOW)))
    assert KNOWN_NO_DEV_JOBS <= jobs, f"{sorted(KNOWN_NO_DEV_JOBS - jobs)} install dev"


def test_a_job_overriding_the_workflow_uv_no_dev_is_not_counted():
    workflow = {
        "env": {"UV_NO_DEV": "1"},
        "jobs": {"kept": {}, "dropped": {"env": {"UV_NO_DEV": ""}}},
    }
    assert set(no_dev_jobs(workflow)) == {"kept"}


def test_the_import_closure_installs_without_the_dev_group(entries):
    paths = [REPO / e for e in sorted(entries)]
    needed = third_party_imports(paths)
    assert {"pyyaml", "jinja2"} <= set(needed), f"the trace found only {sorted(needed)}"
    assert problems(paths, allowed_distributions()) == []


def test_a_dev_only_import_is_flagged(tmp_path):
    script = tmp_path / "entry.py"
    script.write_text("import yaml\n\n\ndef load():\n    import requests\n")
    found = problems([script], allowed_distributions())
    assert any(line.startswith("requests is imported") for line in found), found
