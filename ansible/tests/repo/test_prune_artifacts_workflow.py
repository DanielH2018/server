"""The `prune-artifacts` workflow deletes old artifact versions under promote's own rules.

It holds its master-ref check, its permissions, its own concurrency group, and a cosign pin
and fetch steps equal to promote's, so the two pins cannot drift apart. Its GHCR login runs
through a copy of promote's `RETRY_FUNCTIONS`, held equal the same way. `prune_problems` names
every broken property; the real file must produce none, and each red fixture breaks one.

Run: uv run pytest ansible/tests/repo/test_prune_artifacts_workflow.py
"""

import copy

import pytest
from _helpers import REPO
from test_promote_workflow import (
    PROMOTE_REF,
    REF_GUARD,
    WORKFLOW,
    _checkouts,
    _load,
    _steps,
    _triggers,
)
from test_promote_workflow_retry import LOAD_RETRY

PRUNE_WORKFLOW = REPO / ".github" / "workflows" / "prune-artifacts.yml"
PRUNE_PERMISSIONS = {"contents": "read", "packages": "write"}
PRUNE_COMMAND = "promote.py prune"
LOGIN_STEP = "Log in to GHCR"
COSIGN_PIN = ("COSIGN_VERSION", "COSIGN_SHA256")


def _cosign_steps(wf: dict) -> list[dict]:
    """Every job's cosign cache and fetch steps, in order."""
    return [
        s
        for job in (wf.get("jobs") or {}).values()
        for s in _steps(job)
        if "pinned/cosign" in str(s.get("run", "")) + str(s.get("with", ""))
    ]


def _code_lines(step: dict) -> list[str]:
    lines = (line.strip() for line in str(step.get("run", "")).splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def prune_problems(wf: dict, promote_wf: dict) -> list[str]:
    """Every broken property of the prune workflow, as one line each.

    `promote_wf` supplies the cosign pin and the cache and fetch steps the prune job must
    repeat exactly.
    """
    found = []
    triggers = _triggers(wf)
    if "schedule" not in triggers or "workflow_dispatch" not in triggers:
        found.append("prune: does not trigger on both schedule and workflow_dispatch")
    if wf.get("permissions") != {}:
        found.append("prune: the workflow grants permissions above the job level")
    jobs = wf.get("jobs") or {}
    if list(jobs) != ["prune"]:
        return found + [f"prune: the jobs are {sorted(jobs)}, not ['prune']"]
    job = jobs["prune"]
    if REF_GUARD not in " ".join(str(job.get("if", "")).split()):
        found.append(f"prune: its `if` lacks the master-ref guard {REF_GUARD}")
    if job.get("runs-on") != "ubuntu-26.04":
        found.append(f"prune: runs on {job.get('runs-on')!r}")
    if job.get("permissions") != PRUNE_PERMISSIONS:
        found.append(f"prune: permissions are {job.get('permissions')}")
    if job.get("concurrency") != {"group": "prune", "cancel-in-progress": False}:
        found.append(f"prune: its concurrency is {job.get('concurrency')}")
    checkouts = _checkouts(job)
    args = (checkouts[0].get("with") or {}) if len(checkouts) == 1 else {}
    if args.get("ref") != PROMOTE_REF or args.get("fetch-depth") != 0:
        found.append(f"prune: does not check out {PROMOTE_REF} with fetch-depth: 0")
    if not any(PRUNE_COMMAND in str(s.get("run", "")) for s in _steps(job)):
        found.append(f"prune: runs no `{PRUNE_COMMAND}`")
    cosign, reference = _cosign_steps(wf), _cosign_steps(promote_wf)
    if len(cosign) != 2 or reference != cosign * 2:
        found.append("prune: its cosign fetch steps differ from promote.yml's")
    for key in COSIGN_PIN:
        if (wf.get("env") or {}).get(key) != (promote_wf.get("env") or {}).get(key):
            found.append(f"prune: its {key} differs from promote.yml's")
    functions = (wf.get("env") or {}).get("RETRY_FUNCTIONS")
    if not functions or functions != (promote_wf.get("env") or {}).get(
        "RETRY_FUNCTIONS"
    ):
        found.append("prune: its RETRY_FUNCTIONS differs from promote.yml's")
    for step in _steps(job):
        if any("docker login" in line for line in _code_lines(step)):
            found.append(f"prune: {step.get('name')}: runs docker login outside retry")
    login = next((s for s in _steps(job) if s.get("name") == LOGIN_STEP), {})
    if _code_lines(login) != [LOAD_RETRY, "ghcr_login"]:
        found.append(f"prune: {LOGIN_STEP}: does not run ghcr_login after {LOAD_RETRY}")
    return found


@pytest.fixture(scope="module")
def workflow() -> dict:
    return _load(WORKFLOW)


@pytest.fixture(scope="module")
def prune_workflow() -> dict:
    return _load(PRUNE_WORKFLOW)


def test_the_prune_workflow_holds_every_property(prune_workflow, workflow):
    assert prune_problems(prune_workflow, workflow) == []
    assert len(_cosign_steps(workflow)) == 4


def _prune_in_the_promote_group(wf):
    wf["jobs"]["prune"]["concurrency"]["group"] = "promote"


def _prune_without_the_ref_check(wf):
    wf["jobs"]["prune"]["if"] = "true"


def _prune_with_a_wider_permission(wf):
    wf["jobs"]["prune"]["permissions"]["id-token"] = "write"


def _prune_with_a_drifted_cosign_release(wf):
    wf["env"]["COSIGN_VERSION"] = "v3.1.3"


def _prune_with_a_drifted_cosign_fetch(wf):
    step = _cosign_steps(wf)[1]
    step["run"] = step["run"].replace("--retry 12", "--retry 5")


def _prune_with_a_shallow_checkout(wf):
    _checkouts(wf["jobs"]["prune"])[0]["with"]["fetch-depth"] = 1


def _login_step(wf):
    return next(s for s in wf["jobs"]["prune"]["steps"] if s.get("name") == LOGIN_STEP)


def _prune_with_a_drifted_retry_helper(wf):
    wf["env"]["RETRY_FUNCTIONS"] = wf["env"]["RETRY_FUNCTIONS"].replace("1 2 3", "1 2")


def _prune_without_the_retry_helper(wf):
    del wf["env"]["RETRY_FUNCTIONS"]


def _prune_with_a_bare_login(wf):
    _login_step(wf)["run"] = "docker login ghcr.io --password-stdin"


def _prune_without_loading_retry(wf):
    _login_step(wf)["run"] = "ghcr_login"


def _prune_calling_the_login_without_retry(wf):
    _login_step(wf)["run"] = f"{LOAD_RETRY}\nghcr_login_once"


def _prune_with_the_login_commented_out(wf):
    _login_step(wf)["run"] = f"{LOAD_RETRY}\n# ghcr_login"


def _prune_calling_the_login_before_loading(wf):
    _login_step(wf)["run"] = f"ghcr_login\n{LOAD_RETRY}"


PRUNE_RED = {
    "prune in the promote group": (
        _prune_in_the_promote_group,
        "prune: its concurrency is",
    ),
    "prune without the ref check": (
        _prune_without_the_ref_check,
        "prune: its `if` lacks the master-ref guard",
    ),
    "prune with a wider permission": (
        _prune_with_a_wider_permission,
        "prune: permissions are",
    ),
    "prune with a drifted COSIGN_VERSION": (
        _prune_with_a_drifted_cosign_release,
        "prune: its COSIGN_VERSION differs",
    ),
    "prune with a drifted cosign fetch": (
        _prune_with_a_drifted_cosign_fetch,
        "prune: its cosign fetch steps differ",
    ),
    "prune with a shallow checkout": (
        _prune_with_a_shallow_checkout,
        "prune: does not check out",
    ),
    "prune with a drifted retry helper": (
        _prune_with_a_drifted_retry_helper,
        "prune: its RETRY_FUNCTIONS differs",
    ),
    "prune without the retry helper": (
        _prune_without_the_retry_helper,
        "prune: its RETRY_FUNCTIONS differs",
    ),
    "prune with a bare login": (
        _prune_with_a_bare_login,
        "prune: Log in to GHCR: runs docker login outside retry",
    ),
    "prune without loading retry": (
        _prune_without_loading_retry,
        "prune: Log in to GHCR: does not run ghcr_login",
    ),
    "prune calling the login without retry": (
        _prune_calling_the_login_without_retry,
        "prune: Log in to GHCR: does not run ghcr_login",
    ),
    "prune with the login commented out": (
        _prune_with_the_login_commented_out,
        "prune: Log in to GHCR: does not run ghcr_login",
    ),
    "prune calling the login before loading": (
        _prune_calling_the_login_before_loading,
        "prune: Log in to GHCR: does not run ghcr_login",
    ),
}


@pytest.mark.parametrize("case", PRUNE_RED, ids=list(PRUNE_RED))
def test_each_broken_prune_property_is_named(prune_workflow, workflow, case):
    mutate, expected = PRUNE_RED[case]
    broken = copy.deepcopy(prune_workflow)
    mutate(broken)
    found = prune_problems(broken, workflow)
    assert any(expected in line for line in found), found
