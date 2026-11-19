"""The `prune-artifacts` workflow deletes old artifact versions under promote's own rules.

Its jobs, guard, scopes and concurrency group are rows here, and so are the cosign pin and the
`RETRY_FUNCTIONS` helper it holds equal to promote's, so the two cannot drift apart. Each row's red half is the real file with that one property broken.
"""

import copy

from _promote_workflow import PROMOTE_REF, REF_GUARD
from _row_table import Census, Subject, tracked
from _workflow_rows import PROMOTE, checkouts, condition, jobs, judge, load, mutants
from _workflow_rows import steps, triggers
from test_promote_workflow_retry import LOAD_RETRY

PRUNE = ".github/workflows/prune-artifacts.yml"
PRUNE_PERMISSIONS = {"contents": "read", "packages": "write"}
PRUNE_COMMAND = "promote.py prune"
LOGIN_STEP = "Log in to GHCR"
COSIGN_PIN = ("COSIGN_VERSION", "COSIGN_SHA256")


def prune_row(**kw) -> dict:
    return {
        "files": lambda: tracked(PRUNE),
        "must_find": frozenset({PRUNE}),
        "green": (Subject(PRUNE),),
        **kw,
    }


def _prune(wf: dict) -> dict:
    return jobs(wf).get("prune", {})


def _env(wf: dict) -> dict:
    return wf.get("env") or {}


def _code_lines(step: dict) -> list[str]:
    lines = (line.strip() for line in str(step.get("run", "")).splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def _login_step(wf: dict) -> dict:
    return next((s for s in steps(_prune(wf)) if s.get("name") == LOGIN_STEP), {})


def _set_login(run: str):
    def mutate(wf: dict) -> None:
        _login_step(wf)["run"] = run

    return mutate


def _equal_to_promotes(key: str):
    def rule(wf: dict) -> list[str]:
        if key == "RETRY_FUNCTIONS" and not _env(wf).get(key):
            return [f"{key}: is not set"]
        if _env(wf).get(key) != _env(load(PROMOTE)).get(key):
            return [f"{key}: differs from promote.yml's"]
        return []

    return rule


def _checkout_problems(wf: dict) -> list[str]:
    found = checkouts(_prune(wf))
    args = (found[0].get("with") or {}) if len(found) == 1 else {}
    if args.get("ref") != PROMOTE_REF or args.get("fetch-depth") != 0:
        return [f"does not check out {PROMOTE_REF} with fetch-depth: 0"]
    return []


def _drifted_cosign_release(wf: dict) -> None:
    assert wf["env"]["COSIGN_VERSION"] != "v3.1.3"
    wf["env"]["COSIGN_VERSION"] = "v3.1.3"


PRUNE_ROWS = (
    Census(
        **prune_row(
            name="prune-runs-on-a-schedule-or-a-dispatch",
            reason=(
                "Pruning is a scheduled job with a manual start. A trigger on pushes or pull "
                "requests would delete artifact versions on events nobody scheduled."
            ),
            offence=judge(
                lambda wf: (
                    []
                    if {"schedule", "workflow_dispatch"} <= set(triggers(wf))
                    else ["does not trigger on both schedule and workflow_dispatch"]
                )
            ),
            red=mutants(
                PRUNE, no_schedule_trigger=lambda wf: triggers(wf).pop("schedule")
            ),
        )
    ),
    Census(
        **prune_row(
            name="prune-grants-no-workflow-permissions",
            reason=(
                "The job states its own token scopes. A workflow-level grant reaches every job "
                "added later."
            ),
            offence=judge(
                lambda wf: (
                    []
                    if wf.get("permissions") == {}
                    else ["the workflow grants permissions above the job level"]
                )
            ),
            red=mutants(
                PRUNE,
                a_workflow_level_grant=lambda wf: wf.update(
                    permissions={"contents": "read"}
                ),
            ),
        )
    ),
    Census(
        **prune_row(
            name="prune-is-the-only-job",
            reason=(
                "Every row below reads the job named `prune`. A second job runs with no row "
                "holding its guard, scopes or concurrency group."
            ),
            offence=judge(
                lambda wf: (
                    []
                    if list(jobs(wf)) == ["prune"]
                    else [f"the jobs are {sorted(jobs(wf))}, not ['prune']"]
                )
            ),
            red=mutants(
                PRUNE,
                an_extra_job=lambda wf: wf["jobs"].update(
                    extra=copy.deepcopy(wf["jobs"]["prune"])
                ),
            ),
        )
    ),
    Census(
        **prune_row(
            name="prune-runs-only-on-master",
            reason=(
                "A dispatch can name any ref, and the job deletes artifact versions with a "
                "packages-write token, so it runs only from master's copy of the file."
            ),
            offence=judge(
                lambda wf: (
                    []
                    if REF_GUARD in condition(_prune(wf))
                    else [f"its `if` lacks the master-ref guard {REF_GUARD}"]
                )
            ),
            red=mutants(
                PRUNE,
                prune_without_the_ref_check=lambda wf: wf["jobs"]["prune"].update(
                    {"if": "true"}
                ),
            ),
        )
    ),
    Census(
        **prune_row(
            name="prune-runs-on-the-pinned-runner",
            reason="A floating runner label changes the image under the job with no commit here.",
            offence=judge(
                lambda wf: (
                    []
                    if _prune(wf).get("runs-on") == "ubuntu-26.04"
                    else [f"runs on {_prune(wf).get('runs-on')!r}"]
                )
            ),
            red=mutants(
                PRUNE,
                a_floating_runner=lambda wf: wf["jobs"]["prune"].update(
                    {"runs-on": "ubuntu-latest"}
                ),
            ),
        )
    ),
    Census(
        **prune_row(
            name="prune-holds-only-its-permissions",
            reason=(
                "The job reads the repository and deletes package versions, and holds no "
                "`id-token` or any other scope."
            ),
            offence=judge(
                lambda wf: (
                    []
                    if _prune(wf).get("permissions") == PRUNE_PERMISSIONS
                    else [f"permissions are {_prune(wf).get('permissions')}"]
                )
            ),
            red=mutants(
                PRUNE,
                prune_with_a_wider_permission=lambda wf: wf["jobs"]["prune"][
                    "permissions"
                ].update({"id-token": "write"}),
            ),
        )
    ),
    Census(
        **prune_row(
            name="prune-has-its-own-concurrency-group",
            reason=(
                "Prune never cancels in progress, and it never joins the `promote` group, "
                "where a pending run can be evicted and a prune would queue behind a "
                "promotion."
            ),
            offence=judge(
                lambda wf: (
                    []
                    if _prune(wf).get("concurrency")
                    == {"group": "prune", "cancel-in-progress": False}
                    else [f"its concurrency is {_prune(wf).get('concurrency')}"]
                )
            ),
            red=mutants(
                PRUNE,
                prune_in_the_promote_group=lambda wf: wf["jobs"]["prune"][
                    "concurrency"
                ].update(group="promote"),
            ),
        )
    ),
    Census(
        **prune_row(
            name="prune-checks-out-the-workflows-commit-in-full",
            reason=(
                "The job runs `promote.py prune` from the workflow's own commit, and reads "
                "the full history."
            ),
            offence=judge(_checkout_problems),
            red=mutants(
                PRUNE,
                prune_with_a_shallow_checkout=lambda wf: checkouts(wf["jobs"]["prune"])[
                    0
                ]["with"].update({"fetch-depth": 1}),
            ),
        )
    ),
    Census(
        **prune_row(
            name="prune-runs-the-prune-command",
            reason="A prune job that runs no `promote.py prune` deletes nothing.",
            offence=judge(
                lambda wf: (
                    []
                    if any(
                        PRUNE_COMMAND in str(s.get("run", ""))
                        for s in steps(_prune(wf))
                    )
                    else [f"runs no `{PRUNE_COMMAND}`"]
                )
            ),
            red=mutants(
                PRUNE,
                prune_without_the_command=lambda wf: wf["jobs"]["prune"].update(
                    steps=[
                        s
                        for s in wf["jobs"]["prune"]["steps"]
                        if PRUNE_COMMAND not in str(s.get("run", ""))
                    ]
                ),
            ),
        )
    ),
    Census(
        **prune_row(
            name="prune-cosign-release-equals-promotes",
            reason=(
                "Prune verifies signatures with the cosign that promote's `publish` signs "
                "with, so the two pins (version and sha256) are equal."
            ),
            offence=judge(
                lambda wf: [
                    problem
                    for key in COSIGN_PIN
                    for problem in _equal_to_promotes(key)(wf)
                ]
            ),
            red=mutants(
                PRUNE, prune_with_a_drifted_cosign_release=_drifted_cosign_release
            ),
        )
    ),
    Census(
        **prune_row(
            name="prune-retry-helper-equals-promotes",
            reason=(
                "The GHCR login runs through a copy of promote's `RETRY_FUNCTIONS`, held "
                "equal, so a change to the retry window reaches both workflows."
            ),
            offence=judge(_equal_to_promotes("RETRY_FUNCTIONS")),
            red=mutants(
                PRUNE,
                prune_with_a_drifted_retry_helper=lambda wf: wf["env"].update(
                    RETRY_FUNCTIONS=wf["env"]["RETRY_FUNCTIONS"].replace("1 2 3", "1 2")
                ),
                prune_without_the_retry_helper=lambda wf: wf["env"].pop(
                    "RETRY_FUNCTIONS"
                ),
            ),
        )
    ),
    Census(
        **prune_row(
            name="prune-never-runs-docker-login-bare",
            reason=(
                "A bare `docker login` fails the job on one registry blip. The login goes "
                "through `ghcr_login`, which retries."
            ),
            offence=judge(
                lambda wf: [
                    f"{step.get('name')}: runs docker login outside retry"
                    for step in steps(_prune(wf))
                    if any("docker login" in line for line in _code_lines(step))
                ]
            ),
            red=mutants(
                PRUNE,
                prune_with_a_bare_login=_set_login(
                    "docker login ghcr.io --password-stdin"
                ),
            ),
        )
    ),
    Census(
        **prune_row(
            name="prune-login-loads-retry-then-calls-ghcr-login",
            reason=(
                f"The `{LOGIN_STEP}` step is `{LOAD_RETRY}` then `ghcr_login`, in that order "
                "and uncommented: without the load the function does not exist, and "
                "`ghcr_login_once` does not retry."
            ),
            offence=judge(
                lambda wf: (
                    []
                    if _code_lines(_login_step(wf)) == [LOAD_RETRY, "ghcr_login"]
                    else [f"{LOGIN_STEP}: does not run ghcr_login after {LOAD_RETRY}"]
                )
            ),
            red=mutants(
                PRUNE,
                prune_without_loading_retry=_set_login("ghcr_login"),
                prune_calling_the_login_without_retry=_set_login(
                    f"{LOAD_RETRY}\nghcr_login_once"
                ),
                prune_with_the_login_commented_out=_set_login(
                    f"{LOAD_RETRY}\n# ghcr_login"
                ),
                prune_calling_the_login_before_loading=_set_login(
                    f"ghcr_login\n{LOAD_RETRY}"
                ),
            ),
        )
    ),
)
