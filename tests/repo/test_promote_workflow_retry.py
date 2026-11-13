"""Every `gh` call and GHCR login in the `promote` workflow runs inside its `retry` helper.

One transient 502, 503 or EOF on such a call failed three promote runs between 2026-10-30 and
2026-11-06. `scripts/deploy_tools/tests/test_promote_settle_retry.py` runs the helper itself.

Run: uv run pytest tests/repo/test_promote_workflow_retry.py
"""

import copy
import os
import re
import shutil

import forge
import pytest
from lib.proc_testing import fake_bin
from lib.proc_testing import run as proc_run
from test_promote_workflow import WORKFLOW, _load, _steps

UNRETRIED = re.compile(r"(?<!retry )(?<![\w./-])gh [a-z]+|docker login")
RETRY_CALLERS = re.compile(r"(?<![\w-])(?:retry|ghcr_login)\b")
LOAD_RETRY = 'eval "$RETRY_FUNCTIONS"'
RECORD = "Record prod's digest"
APPLY = "Apply the verdict"
DISPATCH = "Dispatch a run for a newer green commit"
LOGIN = "Log in to GHCR read-only"
# The census must find each of these, so a renamed step cannot empty it.
RETRIED = {
    ("tag", RECORD): "retry gh variable set PROD_DIGEST",
    ("settle", APPLY): "retry gh variable set PROD_DIGEST",
    ("reset-move", RECORD): "retry gh variable set PROD_DIGEST",
    ("settle", DISPATCH): "retry gh workflow run",
    ("promote", LOGIN): "ghcr_login",
}


def retry_problems(wf: dict) -> list[str]:
    """Every call outside the helper, and every step that calls it without loading it."""
    found = []
    functions = str((wf.get("env") or {}).get("RETRY_FUNCTIONS", ""))
    if "retry()" not in functions or "docker login" not in functions:
        found.append("the workflow env's RETRY_FUNCTIONS lacks retry or the GHCR login")
    # A list, because unnamed steps and steps sharing a name would collide as dict keys.
    runs = [
        (job_name, step.get("name") or step.get("id"), str(step.get("run", "")))
        for job_name, job in (wf.get("jobs") or {}).items()
        for step in _steps(job)
    ]
    for job_name, step_name, script in runs:
        code = [
            line for line in script.splitlines() if not line.lstrip().startswith("#")
        ]
        for line in code:
            if match := UNRETRIED.search(line):
                found.append(f"{job_name}: {step_name}: runs {match[0]} outside retry")
        if LOAD_RETRY not in script and RETRY_CALLERS.search("\n".join(code)):
            found.append(f"{job_name}: {step_name}: calls retry without {LOAD_RETRY}")
    for (job_name, step_name), call in RETRIED.items():
        if not any(
            (job, name) == (job_name, step_name) and call in script
            for job, name, script in runs
        ):
            found.append(f"{job_name}: {step_name}: does not run {call}")
    return found


@pytest.fixture(scope="module")
def workflow() -> dict:
    return _load(WORKFLOW)


def test_every_call_runs_inside_retry_is_clean(workflow):
    assert retry_problems(workflow) == []


RED = {
    "tag's PROD_DIGEST write": ("tag", RECORD, "retry gh", "gh", "runs gh variable"),
    "settle's PROD_DIGEST write": (
        "settle",
        APPLY,
        "retry gh variable set PROD_DIGEST",
        "gh variable set PROD_DIGEST",
        "runs gh variable",
    ),
    "reset-move's PROD_DIGEST write": (
        "reset-move",
        RECORD,
        "retry gh",
        "gh",
        "runs gh variable",
    ),
    "the dispatch": (
        "settle",
        DISPATCH,
        "retry gh workflow run",
        "gh workflow run",
        "runs gh workflow",
    ),
    "the run list read": ("settle", DISPATCH, "retry gh api", "gh api", "runs gh api"),
    "the GHCR login": (
        "promote",
        LOGIN,
        "ghcr_login",
        "docker login ghcr.io",
        "runs docker login",
    ),
    "a step that never loads retry": ("settle", APPLY, LOAD_RETRY, "", "without eval"),
}


@pytest.mark.parametrize("case", RED, ids=list(RED))
def test_a_call_outside_retry_is_flagged(workflow, case):
    job, step_name, old, new, expected = RED[case]
    broken = copy.deepcopy(workflow)
    step = next(s for s in broken["jobs"][job]["steps"] if s.get("name") == step_name)
    step["run"] = step["run"].replace(old, new)
    assert (
        step["run"]
        != next(
            s for s in workflow["jobs"][job]["steps"] if s.get("name") == step_name
        )["run"]
    ), f"{case}: the mutation left the step unchanged"
    assert any(
        line.startswith(f"{job}: {step_name}: ") and expected in line
        for line in retry_problems(broken)
    ), retry_problems(broken)


BARE = "gh variable set PROD_DIGEST --body x"


@pytest.mark.parametrize(
    "added",
    [
        [{"run": BARE}, {"uses": "actions/checkout@v4"}],
        [{"name": "Same", "run": BARE}, {"name": "Same", "run": "true"}],
    ],
    ids=["unnamed-step-before-a-uses-step", "step-sharing-a-later-step-s-name"],
)
def test_a_bare_call_in_a_step_with_a_colliding_name_is_flagged(workflow, added):
    broken = copy.deepcopy(workflow)
    broken["jobs"]["tag"]["steps"] += added
    assert any(
        line.startswith("tag: ") and "runs gh variable outside retry" in line
        for line in retry_problems(broken)
    ), retry_problems(broken)


# GHCR refused a second signature on an already-signed digest with `write_package` (run
# 42710237024), so a second publish of one commit must find the signature and not sign again.
SIGN = "retry sign_unless_signed"
PUBLISH_GROUP = "publish-${{ needs.render.outputs.sha }}"


def _push(wf: dict) -> dict:
    publish = (wf.get("jobs") or {}).get("publish", {})
    return next((s for s in _steps(publish) if s.get("id") == "push"), {})


def resign_problems(wf: dict) -> list[str]:
    """Every way a second publish of one commit could sign its digest again."""
    publish = (wf.get("jobs") or {}).get("publish", {})
    push = _push(wf)
    env, script = push.get("env") or {}, str(push.get("run", ""))
    found = []
    if SIGN not in script:
        found.append(f"publish: the signed push does not run {SIGN}")
    verify, sign = script.find("cosign verify"), script.find("cosign sign")
    if verify < 0 or sign < verify:
        found.append("publish: the signed push signs without verifying first")
    if (env.get("IDENTITY"), env.get("ISSUER")) != (
        forge.SIGNER_IDENTITY,
        forge.SIGNER_ISSUER,
    ):
        found.append(
            "publish: the signed push verifies under an identity forge.py lacks"
        )
    concurrency = publish.get("concurrency") or {}
    if (concurrency.get("group"), concurrency.get("cancel-in-progress")) != (
        PUBLISH_GROUP,
        False,
    ):
        found.append(f"publish: has no concurrency group {PUBLISH_GROUP}")
    return found


def test_a_second_publish_does_not_sign_again_is_clean(workflow):
    assert resign_problems(workflow) == []


def _drop_verify(wf):
    push = _push(wf)
    push["run"] = re.sub(
        r"if cosign verify.*?\n\s*fi\n", "", push["run"], flags=re.DOTALL
    )


def _drifted_identity(wf):
    _push(wf)["env"]["IDENTITY"] = forge.SIGNER_IDENTITY.replace("promote", "ci")


def _drop_group(wf):
    del wf["jobs"]["publish"]["concurrency"]


def _bare_sign(wf):
    push = _push(wf)
    push["run"] = push["run"].replace(SIGN, "sign_unless_signed")


RESIGN_RED = {
    "a sign with no verify before it": (_drop_verify, "signs without verifying"),
    "a verify under a drifted identity": (_drifted_identity, "an identity forge.py"),
    "a publish job with no group": (_drop_group, "no concurrency group"),
    "a sign outside retry": (_bare_sign, f"does not run {SIGN}"),
}


@pytest.mark.parametrize("case", RESIGN_RED, ids=list(RESIGN_RED))
def test_a_publish_that_could_sign_again_is_flagged(workflow, case):
    mutate, expected = RESIGN_RED[case]
    broken = copy.deepcopy(workflow)
    mutate(broken)
    assert any(expected in line for line in resign_problems(broken)), resign_problems(
        broken
    )


DIGEST = "sha256:" + "5b" * 32
FAKE_FLUX = f"""#!/usr/bin/env bash
echo '{{"digest":"{DIGEST}"}}'
"""
FAKE_COSIGN = """#!/usr/bin/env bash
case "$1" in
  verify) exit "${FAKE_VERIFY_RC:-1}" ;;
  sign) echo "$*" >> "$SIGN_LOG"; exit 0 ;;
esac
"""


@pytest.mark.parametrize(
    ("verify_rc", "signs"),
    [("0", 0), ("1", 1)],
    ids=["an-already-signed-digest", "an-unsigned-digest"],
)
def test_the_push_signs_only_a_digest_that_does_not_verify(
    workflow, tmp_path, verify_rc, signs
):
    assert shutil.which("jq"), "the step reads flux's digest with jq"
    log = tmp_path / "signs.log"
    env = {
        **os.environ,
        "RETRY_FUNCTIONS": workflow["env"]["RETRY_FUNCTIONS"],
        **{k: str(v) for k, v in _push(workflow)["env"].items()},
        "SHA": "ab" * 20,
        "GITHUB_REPOSITORY": "owner/repo",
        "GH_ACTOR": "actor",
        "GITHUB_TOKEN": "fake",
        "FAKE_VERIFY_RC": verify_rc,
        "SIGN_LOG": str(log),
    }
    stubs = fake_bin(
        tmp_path / "bin", flux=FAKE_FLUX, cosign=FAKE_COSIGN, docker="exit 0\n"
    )
    result = proc_run(["bash", "-c", _push(workflow)["run"]], env=env, stub_bin=stubs)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = log.read_text().splitlines() if log.exists() else []
    assert len(calls) == signs, calls
