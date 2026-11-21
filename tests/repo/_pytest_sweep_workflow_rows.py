"""The nightly sweep workflow and the pytest-tools action, as census rows.

`test_census_rows_workflows.py` runs `PYTEST_SWEEP_ROWS`. Each row's red half is the real file
with one property broken.
"""

from collections.abc import Callable

from _ci_job_rows import RESTORE, SAVE, rewrite_step
from _row_table import Census, Subject, tracked
from _workflow_rows import (
    Workflow,
    condition,
    jobs,
    judge,
    mutants,
    runs,
    steps,
    triggers,
)
from ci_verdict import CI_GATE_NAME

SWEEP = ".github/workflows/pytest-sweep.yml"
PYTEST_TOOLS = ".github/actions/pytest-tools/action.yml"
RUN_STEP = "Run the whole test suite"
CACHE_STEP = "Cache the age and apt-utils binaries"
INSTALL_STEP = "Install age and apt-utils for the tests"
IMAGE_VERSION = "steps.image.outputs.version"


def sweep_step(wf: Workflow, name: str) -> dict | None:
    return next(
        (s for s in steps(jobs(wf).get("sweep", {})) if s.get("name") == name), None
    )


def _sweep_run_problems(wf: Workflow) -> list[str]:
    found = []
    declared = triggers(wf)
    if not declared.get("schedule"):
        found.append("the sweep has no schedule")
    if {"push", "pull_request"} & set(declared):
        found.append("the sweep runs per commit, where ci.yml's selection already runs")
    suite = sweep_step(wf, RUN_STEP)
    if suite is None:
        found.append(f"the sweep job has no {RUN_STEP!r} step")
    elif "--changed-since" in runs(suite) or "pytest_shard" in runs(suite):
        found.append("the sweep hands pytest a selection or a shard")
    if not any(
        str(s.get("uses", "")).endswith("/pytest-tools")
        for s in steps(jobs(wf).get("sweep", {}))
    ):
        found.append("the sweep does not install the tools ci.yml's legs install")
    return found


def _sweep_grading_problems(wf: Workflow) -> list[str]:
    group = str((wf.get("concurrency") or {}).get("group", ""))
    found = []
    if "github.workflow" not in group or "github.sha" in group:
        found.append(
            f"concurrency group {group!r} can join ci.yml's group for a commit"
        )
    for job_id, job in jobs(wf).items():
        if job.get("name") == CI_GATE_NAME:
            found.append(f"job {job_id} reports under the name of ci.yml's gate")
        writes = (job.get("permissions") or {}).get("issues") == "write"
        if writes and any("pytest" in runs(s) for s in steps(job)):
            found.append(f"job {job_id} runs the suite with a token that writes issues")
    return found


def _sweep_report_problems(wf: Workflow) -> list[str]:
    condition = str(jobs(wf).get("report", {}).get("if", ""))
    found = []
    if "github.ref == 'refs/heads/master'" not in condition:
        found.append("the report job does not stop at master")
    if "always()" not in condition:
        found.append("a red sweep does not reach the report")
    return found


def _sweep_save_problems(wf: Workflow) -> list[str]:
    job = jobs(wf).get("sweep", {})
    save = rewrite_step(job, SAVE)
    if save is None:
        return [f"the sweep job has no {SAVE} step for pytest's rewritten modules"]
    found = []
    suite = sweep_step(wf, RUN_STEP)
    if suite is None or steps(job).index(save) < steps(job).index(suite):
        found.append(f"the save step does not run after {RUN_STEP!r}")
    gate = condition(save)
    if "github.ref == 'refs/heads/master'" not in gate:
        found.append(f"the save gate {gate!r} saves a branch's entry")
    if "!cancelled()" not in gate:
        found.append(f"the save gate {gate!r} saves nothing after a red sweep")
    if "github.run_id" not in str(save["with"].get("key")):
        found.append("the save key repeats across runs, so a later save writes nothing")
    if rewrite_step(job, RESTORE) is not None:
        found.append(
            "the sweep restores an earlier entry and carries its stale files on"
        )
    return found


def _action_step(action: Workflow, name: str) -> dict:
    return next(
        (s for s in steps(action.get("runs") or {}) if s.get("name") == name), {}
    )


def _apt_only_without_working_copies(run: str) -> bool:
    """Whether every apt-get line sits in the `else` of the check that the copies run."""
    in_else, guarded = False, []
    for line in run.splitlines():
        text = line.strip()
        if text.startswith("if cached_apt_tools_run"):
            in_else = False
        elif text == "else":
            in_else = True
        elif text == "fi":
            in_else = False
        elif "apt-get" in text:
            guarded.append(in_else)
    return bool(guarded) and all(guarded)


def _apt_cache_problems(action: Workflow) -> list[str]:
    cache = _action_step(action, CACHE_STEP)
    found = []
    if IMAGE_VERSION not in str((cache.get("with") or {}).get("key", "")):
        found.append("the cache key does not carry the runner image version")
    if IMAGE_VERSION not in str(cache.get("if", "")):
        found.append("the cache step runs on a runner that names no image version")
    return found


def _apt_fallback_problems(action: Workflow) -> list[str]:
    run = runs(_action_step(action, INSTALL_STEP))
    if _apt_only_without_working_copies(run):
        return []
    return ["apt-get runs whether or not the cached copies work"]


def _sudo_problems(action: Workflow) -> list[str]:
    return [
        f"step {s.get('name')!r} runs sudo, which a runner pod under `restricted` cannot"
        for s in steps(action.get("runs") or {})
        if "sudo" in runs(s)
    ]


def sweep_job(change: Callable[[dict], object]) -> Callable[[Workflow], object]:
    return lambda wf: change(jobs(wf)["sweep"])


def _rename_sweep_step(old: str, new: str) -> Callable[[Workflow], object]:
    return sweep_job(
        lambda job: [s.update(name=new) for s in steps(job) if s.get("name") == old]
    )


def _run_a_selection(job: dict) -> None:
    for step in steps(job):
        if step.get("name") == RUN_STEP:
            step["run"] += (
                "uv run python scripts/dev/pytest_shard.py --changed-since HEAD^1\n"
            )


def _drop_the_tools(job: dict) -> None:
    job["steps"].remove(
        next(s for s in steps(job) if "pytest-tools" in str(s.get("uses")))
    )


def _edit_run(name: str, find: str, replace: str) -> Callable[[Workflow], None]:
    def mutate(action: Workflow) -> None:
        step = _action_step(action, name)
        assert find in step["run"], f"{name!r} no longer holds {find!r}"
        step["run"] = step["run"].replace(find, replace)

    return mutate


def _save_step(job: dict) -> dict:
    """The sweep job's rewrite-cache save step, for a mutation that needs one to change."""
    save = rewrite_step(job, SAVE)
    assert save is not None, "the sweep job has no rewrite-cache save step"
    return save


def _move_save_before_the_suite(job: dict) -> None:
    save = _save_step(job)
    job["steps"].remove(save)
    job["steps"].insert(0, save)


def _restore_before_the_suite(job: dict) -> None:
    save = _save_step(job)
    job["steps"].insert(0, {**save, "uses": save["uses"].replace(SAVE, RESTORE)})


def _edit_save(**fields: object) -> Callable[[Workflow], object]:
    return sweep_job(lambda job: _save_step(job).update(fields))


def _row(path: str, **row) -> dict:
    return {
        "files": lambda: tracked(path),
        "must_find": frozenset({path}),
        "green": (Subject(path),),
        **row,
    }


PYTEST_SWEEP_ROWS = (
    Census(
        **_row(
            SWEEP,
            name="pytest-sweep-runs-the-whole-suite-nightly",
            reason=(
                "ci.yml's pytest legs run a pull request's selection, so `pytest-sweep.yml` is "
                "the run that finds a module the selection table left out. It runs on a "
                "schedule, never per commit where ci.yml's selection already runs, with no "
                "selection or shard handed to pytest, and with the tools ci.yml's legs install."
            ),
            offence=judge(_sweep_run_problems),
            red=mutants(
                SWEEP,
                no_schedule=lambda wf: triggers(wf).pop("schedule"),
                a_per_push_run=lambda wf: triggers(wf).update(push=None),
                a_per_pull_request_run=lambda wf: triggers(wf).update(
                    pull_request=None
                ),
                a_sweep_that_runs_a_selection=sweep_job(_run_a_selection),
                a_sweep_without_the_suite_step=_rename_sweep_step(RUN_STEP, "Run"),
                a_sweep_without_the_tools=sweep_job(_drop_the_tools),
            ),
        )
    ),
    Census(
        **_row(
            SWEEP,
            name="pytest-sweep-result-cannot-grade-a-commit",
            reason=(
                "A red sweep is on whatever SHA is master's head when GitHub starts it, 8 to 10 "
                "hours after the cron time, so it is not that commit's verdict. `ci_verdict` "
                "takes the worst check-run per required name on a commit, so a job under the "
                "name of ci.yml's gate would hold every landing behind a SHA whose own run was "
                "green. A concurrency group that joins ci.yml's for a commit lets the sweep "
                "cancel its run. `issues: write` sits on the report job alone, which runs one "
                "script and no test."
            ),
            offence=judge(_sweep_grading_problems),
            red=mutants(
                SWEEP,
                a_group_keyed_on_the_commit=lambda wf: wf["concurrency"].update(
                    group="${{ github.workflow }}-${{ github.sha }}"
                ),
                a_group_not_keyed_on_the_workflow=lambda wf: wf["concurrency"].update(
                    group="${{ github.ref }}"
                ),
                a_job_under_the_gates_name=sweep_job(
                    lambda job: job.update(name=CI_GATE_NAME)
                ),
                a_suite_job_that_writes_issues=sweep_job(
                    lambda job: job.update(permissions={"issues": "write"})
                ),
            ),
        )
    ),
    Census(
        **_row(
            SWEEP,
            name="pytest-sweep-only-a-run-on-master-reports",
            reason=(
                "The issue speaks for master: a dispatch on a branch reports nothing. A red "
                "sweep must still reach the report, so the job's condition keeps `always()`."
            ),
            offence=judge(_sweep_report_problems),
            red=mutants(
                SWEEP,
                a_report_that_skips_a_red_sweep=lambda wf: jobs(wf)["report"].update(
                    {"if": "${{ github.ref == 'refs/heads/master' }}"}
                ),
                a_report_from_any_branch=lambda wf: jobs(wf)["report"].update(
                    {"if": "${{ always() }}"}
                ),
            ),
        )
    ),
    Census(
        **_row(
            PYTEST_TOOLS,
            name="pytest-tools-apt-cache-key-carries-the-image-version",
            reason=(
                "A leg copies the binaries apt-get installed into `$RUNNER_TEMP/pinned/apt-tools`, "
                "and a later leg installs those copies instead of running apt-get (#8684). The "
                "copies link against the image's own libraries, so the cache key carries the "
                "image version. A runner that names no image version skips the cache step."
            ),
            offence=judge(_apt_cache_problems),
            red=mutants(
                PYTEST_TOOLS,
                a_key_without_the_image_version=lambda a: _action_step(a, CACHE_STEP)[
                    "with"
                ].update(key="apt-tools-${{ runner.os }}"),
                a_cache_step_with_no_image_condition=lambda a: _action_step(
                    a, CACHE_STEP
                ).pop("if"),
            ),
        )
    ),
    Census(
        **_row(
            PYTEST_TOOLS,
            name="pytest-tools-apt-get-is-the-fallback-branch",
            reason=(
                "`apt-get` stays as the branch a leg takes when the cached copies are absent or "
                "do not run. An `apt-get` outside that `else` runs on every leg and spends the "
                "install time the cache saves (#8684)."
            ),
            offence=judge(_apt_fallback_problems),
            red=mutants(
                PYTEST_TOOLS,
                an_apt_get_outside_the_fallback=_edit_run(
                    INSTALL_STEP,
                    "if cached_apt_tools_run; then",
                    "if true; then\n  apt-get install -y age",
                ),
            ),
        )
    ),
    Census(
        **_row(
            PYTEST_TOOLS,
            name="pytest-tools-runs-no-sudo",
            reason=(
                "No step runs `sudo`, so the action runs unchanged in a runner pod with no "
                "root, which `restricted` Pod Security admits (docs/ci-throughput.md, Phase C)."
            ),
            offence=judge(_sudo_problems),
            red=mutants(
                PYTEST_TOOLS,
                a_sudo_apt_get_update=_edit_run(
                    INSTALL_STEP,
                    'apt-get -q "${private[@]}" update',
                    "sudo apt-get update -q",
                ),
            ),
        )
    ),
    Census(
        **_row(
            SWEEP,
            name="pytest-sweep-saves-the-rewrite-cache-ci-restores",
            reason=(
                "The sweep is the one writer of ci.yml's rewrite cache: it collects every "
                "module, so its entry holds whatever a leg's shard runs, and an entry saved on "
                "master is readable by every branch. It saves after the suite has written the "
                "files, on a red sweep too, under a key unique to the run. It restores nothing, "
                "so an entry never carries a deleted module's file or one named for an older "
                "pytest."
            ),
            offence=judge(_sweep_save_problems),
            red=mutants(
                SWEEP,
                a_save_on_every_branch=_edit_save(**{"if": "${{ !cancelled() }}"}),
                a_save_only_after_a_green_sweep=_edit_save(
                    **{"if": "${{ github.ref == 'refs/heads/master' }}"}
                ),
                a_save_under_a_fixed_key=_edit_save(
                    **{"with": {"path": "**/__pycache__/*-pytest-*.pyc", "key": "k"}}
                ),
                a_save_before_the_suite=sweep_job(_move_save_before_the_suite),
                a_restore_before_the_suite=sweep_job(_restore_before_the_suite),
                a_sweep_with_no_save=sweep_job(
                    lambda job: job["steps"].remove(_save_step(job))
                ),
            ),
        )
    ),
)
