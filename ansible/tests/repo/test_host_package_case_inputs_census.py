"""CASE_INPUTS, the host_packages_build job's list of the container cases' inputs, against the files.

The render step in ci.yml sets `cases=true` when a PR changes a path CASE_INPUTS names, and the
container steps run on it as they run on a differing render (#5004). The list is the one
place those inputs are named, so an input missing from it is an input whose PR skips the cases.
`test_host_package_cases_run_on_their_inputs.py` proves the trigger fires for each input; this
module proves the list holds every input there is: the cases script, every `validate` module it
imports, and every tracked file in the test image's build context.

Run: uv run pytest ansible/tests/repo/test_host_package_case_inputs_census.py
"""

import ast
import re
from pathlib import Path

from _helpers import REPO
from lib import yaml_fast
from lib.git_testing import git_out

CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
# The render step sits in the build job and the image step in the cases legs (#6151).
JOBS = ("host_packages_build", "host_packages")
CASES_SCRIPT = "scripts/validate/host_package_cases.py"
DOCKERFILE = "hosts/lib/tests/container/Dockerfile"
IMAGE_STEP = "Build the test image for this release"
# The package whose modules are inputs of the cases alone. The renderer is a render input, which
# the render diff already sees, and `lib` is shared by every script in the repository.
_CASE_PACKAGES = ("validate",)


def _job_steps() -> list[dict]:
    jobs = yaml_fast.safe_load(CI_WORKFLOW.read_text())["jobs"]
    return [step for job in JOBS for step in jobs[job]["steps"]]


def _case_input_pathspecs() -> list[str]:
    (render,) = [step for step in _job_steps() if step.get("id") == "render"]
    return render["env"]["CASE_INPUTS"].split()


def _imported_modules(script: Path) -> set[str]:
    """Every module under `_CASE_PACKAGES` that `script` imports, transitively, repo-relative.

    `from a.b import c` reaches `a/b.py` when that is a module and `a/b/c.py` when `c` is one.
    """
    found: set[str] = set()
    pending = [script]
    while pending:
        for node in ast.walk(ast.parse(pending.pop().read_text())):
            if not (isinstance(node, ast.ImportFrom) and node.module):
                continue
            if node.module.split(".")[0] not in _CASE_PACKAGES:
                continue
            base = REPO / "scripts" / node.module.replace(".", "/")
            candidates = [base.with_suffix(".py")]
            candidates += [base / f"{alias.name}.py" for alias in node.names]
            for path in candidates:
                relative = str(path.relative_to(REPO))
                if path.is_file() and relative not in found:
                    found.add(relative)
                    pending.append(path)
    return found


def _matches(path: str, spec: str) -> bool:
    """Whether pathspec `spec` names `path`: a `/`-ended spec by prefix, any other exactly."""
    return path.startswith(spec) if spec.endswith("/") else path == spec


def _uncovered(files: set[str], pathspecs: list[str]) -> list[str]:
    """The members of `files` that no pathspec in `pathspecs` names."""
    return sorted(
        path for path in files if not any(_matches(path, spec) for spec in pathspecs)
    )


def _case_input_members() -> set[str]:
    """What CASE_INPUTS must cover: the cases script, its imports and the image's context."""
    (image,) = [step for step in _job_steps() if step.get("name") == IMAGE_STEP]
    match = re.search(
        r"docker build\b.*?-t \S+ (\S+)", image["run"].replace("\\\n", " ")
    )
    assert match, f"no `docker build ... -t <tag> <context>` in {IMAGE_STEP!r}"
    context = match.group(1).rstrip("/")
    return {
        CASES_SCRIPT,
        *_imported_modules(REPO / CASES_SCRIPT),
        *git_out(REPO, "ls-files", context).splitlines(),
    }


def test_case_inputs_cover_the_cases_script_its_imports_and_the_image_context() -> None:
    members = _case_input_members()
    named = {
        "scripts/validate/validate_lib/host_fixtures.py",
        "scripts/validate/validate_lib/host_container.py",
        DOCKERFILE,
    }
    assert named <= members, (
        f"the census lost {sorted(named - members)}; the import walk or the build-context "
        f"parse no longer reaches them, so the check below proves nothing about them"
    )
    missing = _uncovered(members, _case_input_pathspecs())
    assert not missing, (
        f"CASE_INPUTS on {JOBS[0]}'s render step does not list {missing}; a PR changing only "
        f"one of them changes no render and skips the container cases (#5004)"
    )


def test_every_case_input_pathspec_matches_a_tracked_file() -> None:
    tracked = git_out(REPO, "ls-files").splitlines()
    stale = [
        spec
        for spec in _case_input_pathspecs()
        if not any(_matches(path, spec) for path in tracked)
    ]
    assert not stale, f"CASE_INPUTS lists {stale}, which match no tracked file"


def test_dropping_any_case_input_leaves_a_member_uncovered() -> None:
    """The red half, on the real list: every CASE_INPUTS entry covers a member nothing else does.

    Dropping an entry must fail the coverage check above, so the check reads the list ci.yml
    holds rather than passing on any list.
    """
    members = _case_input_members()
    pathspecs = _case_input_pathspecs()
    assert "scripts/validate/validate_lib/host_fixtures.py" in pathspecs, pathspecs
    unneeded = [
        spec
        for spec in pathspecs
        if not _uncovered(members, [s for s in pathspecs if s != spec])
    ]
    assert not unneeded, (
        f"dropping {unneeded} from CASE_INPUTS leaves every member covered, so the coverage "
        f"check cannot go red on that entry"
    )


def test_a_directory_named_without_its_slash_matches_nothing_under_it() -> None:
    fixture = "hosts/lib/tests/container/fixture/base.yml"
    assert _uncovered({fixture}, ["hosts/lib/tests/container/"]) == []
    assert _uncovered({fixture}, ["hosts/lib/tests/container"]) == [fixture]
