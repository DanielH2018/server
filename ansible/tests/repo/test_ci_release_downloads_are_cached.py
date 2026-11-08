"""Every release download in ci.yml sits behind an actions/cache keyed on its pin.

On 2026-11-08 a GitHub release asset answered HTTP 504 for over an hour (#7625), longer than
any step's `curl --retry` window, so each job that downloaded a pinned binary failed. With the
download cached under its version, a run fetches it only when the pin moves.

A step that curls holds three properties: an earlier step of its job caches the
`$RUNNER_TEMP/pinned/<tool>` directory the step writes to, that cache's key reads the pin
rather than a constant, and every curl sits in the branch that runs when the cached file is
absent. A step running a script that downloads into its own cache directory has an earlier
cache of that directory instead.

Run: uv run pytest ansible/tests/repo/test_ci_release_downloads_are_cached.py
"""

import copy
import re

import pytest
from _helpers import REPO
from lib import yaml_fast

CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
CACHE = "actions/cache@"
PINNED = re.compile(r"\$RUNNER_TEMP/pinned/([\w-]+)")
PIN_EXPRESSION = re.compile(r"\$\{\{[^}]*\b(?:env\.|steps\.|hashFiles\()")
# The scripts that fetch into a cache directory of their own, and that directory.
SCRIPT_CACHES = {
    "scripts/validate/alert_tools.sh": ".git/alert-tools",
    "scripts/validate/vale.sh": "styles/Google",
}
# Steps the census must find, so a reshaped workflow cannot pass empty.
KNOWN_DOWNLOADS = {
    ("hooks", "Install Vale"),
    ("hooks", "Provision promtool, amtool, pint and lokitool"),
    ("pytest", "Install sops and age for the cluster_vars tests"),
    ("sops_manifest_type_proof", "Install sops, age and kind"),
    ("host_packages_build", "Fetch the pinned nfpm and check its sha256"),
    ("host_packages_build", "Scan the named package roots for secrets"),
}


def _cache_paths(step: dict) -> list[str]:
    raw = str((step.get("with") or {}).get("path", ""))
    return [
        line.strip().replace("${{ runner.temp }}", "$RUNNER_TEMP").removesuffix("/*")
        for line in raw.splitlines()
        if line.strip() and not line.strip().startswith("!")
    ]


def _unguarded_curls(run: str) -> list[str]:
    """Curl lines not inside the `else` of an `if [[ -f ... ]]` cache check."""
    found, stack = [], []
    for line in run.splitlines():
        text = line.strip()
        if text.startswith("if "):
            stack.append(["-f" in text, False])
        elif text == "else" and stack:
            stack[-1][1] = True
        elif text == "fi" and stack:
            stack.pop()
        elif text.startswith("curl "):
            if not (stack and stack[-1][0] and stack[-1][1]):
                found.append(text)
    return found


def downloads(workflow: dict) -> set[tuple[str, str]]:
    """Every (job, step name) whose run curls or calls a downloading script."""
    found = set()
    for job_name, job in workflow["jobs"].items():
        for step in job.get("steps") or []:
            run = str(step.get("run", ""))
            if re.search(r"^\s*curl ", run, re.MULTILINE) or any(
                s in run for s in SCRIPT_CACHES
            ):
                found.add((job_name, str(step.get("name"))))
    return found


def problems(workflow: dict) -> list[str]:
    found = []
    for job_name, job in workflow["jobs"].items():
        cached: dict[str, str] = {}
        for step in job.get("steps") or []:
            if str(step.get("uses", "")).startswith(CACHE):
                key = str((step.get("with") or {}).get("key", ""))
                for path in _cache_paths(step):
                    cached[path] = key
                continue
            run = str(step.get("run", ""))
            where = f"{job_name}: {step.get('name')}"
            needs = [f"$RUNNER_TEMP/pinned/{tool}" for tool in PINNED.findall(run)]
            needs += [d for s, d in SCRIPT_CACHES.items() if s in run]
            if re.search(r"^\s*curl ", run, re.MULTILINE) and not PINNED.search(run):
                found.append(f"{where}: curls outside $RUNNER_TEMP/pinned/")
            for path in dict.fromkeys(needs):
                if path not in cached:
                    found.append(
                        f"{where}: no earlier actions/cache step caches {path}"
                    )
                elif not PIN_EXPRESSION.search(cached[path]):
                    found.append(
                        f"{where}: the cache key of {path} does not read the pin"
                    )
            for line in _unguarded_curls(run):
                found.append(f"{where}: curls without checking the cache first: {line}")
    return found


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml_fast.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))


def _job_steps(workflow: dict, job: str) -> list[dict]:
    return workflow["jobs"][job]["steps"]


def _step(workflow: dict, job: str, name: str) -> dict:
    return next(s for s in _job_steps(workflow, job) if s.get("name") == name)


def test_the_census_finds_the_known_downloads(workflow):
    assert KNOWN_DOWNLOADS <= downloads(workflow), sorted(downloads(workflow))


def test_every_release_download_is_cached(workflow):
    assert problems(workflow) == []


def test_a_download_without_its_cache_is_flagged(workflow):
    broken = copy.deepcopy(workflow)
    steps = _job_steps(broken, "host_packages_build")
    steps.remove(_step(broken, "host_packages_build", "Cache the nfpm release"))
    assert problems(broken) == [
        "host_packages_build: Fetch the pinned nfpm and check its sha256: "
        "no earlier actions/cache step caches $RUNNER_TEMP/pinned/nfpm"
    ]


def test_a_constant_cache_key_is_flagged(workflow):
    broken = copy.deepcopy(workflow)
    _step(broken, "pytest", "Cache the sops release")["with"]["key"] = "sops-linux"
    assert any(
        "the cache key of $RUNNER_TEMP/pinned/sops does not read the pin" in p
        for p in problems(broken)
    ), problems(broken)


def test_a_curl_that_ignores_the_cache_is_flagged(workflow):
    broken = copy.deepcopy(workflow)
    step = _step(broken, "pytest", "Install sops and age for the cluster_vars tests")
    step["run"] = step["run"].replace('if [[ -f "$sops" ]]; then', "if true; then")
    assert any("curls without checking the cache first" in p for p in problems(broken))


def test_a_script_download_without_its_cache_is_flagged(workflow):
    broken = copy.deepcopy(workflow)
    steps = _job_steps(broken, "hooks")
    steps.remove(_step(broken, "hooks", "Cache promtool, amtool, pint and lokitool"))
    assert any(
        "no earlier actions/cache step caches .git/alert-tools" in p
        for p in problems(broken)
    ), problems(broken)


def test_a_curl_outside_the_pinned_tree_is_flagged():
    workflow = {
        "jobs": {
            "j": {"steps": [{"name": "s", "run": "curl -fsSLo /tmp/x https://e/x\n"}]}
        }
    }
    assert "j: s: curls outside $RUNNER_TEMP/pinned/" in problems(workflow)
