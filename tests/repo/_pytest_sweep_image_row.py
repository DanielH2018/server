"""The census row holding that the nightly sweep runs the tests marked `image`."""

from collections.abc import Callable

from _pytest_sweep_workflow_rows import RUN_STEP, SWEEP, sweep_job, sweep_step
from _row_table import Census, Subject, tracked
from _workflow_rows import Workflow, condition, jobs, judge, mutants, runs, steps

IMAGE_STEP = "Run the image-marked tests"
LIST_STEP = "List the modules that failed"
IMAGE_LEG_PATHS = " tests/longhorn/ k8s/roles/crowdsec/tests/test_jobs_image.py"


def _pytest_arguments(run: str) -> list[str]:
    """The words of the step's pytest command, up to the pipe into `tee`."""
    line = next((ln for ln in run.splitlines() if "pytest" in ln), "")
    return line.split("|")[0].split()


def _image_step_problems(wf: Workflow) -> list[str]:
    step = sweep_step(wf, IMAGE_STEP)
    if step is None:
        return [f"the sweep job has no {IMAGE_STEP!r} step"]
    run, order = runs(step), steps(jobs(wf)["sweep"])
    words = _pytest_arguments(run)
    found = []
    if "-m image" not in " ".join(words):
        found.append("the image step does not select `-m image`")
    if "-n 0" not in " ".join(words):
        found.append("the image step lets xdist workers pull the images in parallel")
    if paths := [w for w in words if "/" in w]:
        found.append(f"the image step collects a path list {paths}, which can drift")
    if "!cancelled()" not in condition(step):
        found.append("the image step is skipped on every night the suite is red")
    if "tee -a" not in run or "sweep.log" not in run:
        found.append("the image step does not append to the log the list step reads")
    suite, listing = sweep_step(wf, RUN_STEP), sweep_step(wf, LIST_STEP)
    if suite is None or order.index(step) < order.index(suite):
        found.append(f"the image step does not run after {RUN_STEP!r}")
    if listing is None or order.index(step) > order.index(listing):
        found.append(f"the image step does not run before {LIST_STEP!r}")
    return found


def _image_step(job: dict) -> dict:
    """The sweep job's image step, for a mutation that needs one to change."""
    step = next((s for s in steps(job) if s.get("name") == IMAGE_STEP), None)
    assert step is not None, f"the sweep job has no {IMAGE_STEP!r} step"
    return step


def _edit_image_run(find: str, replace: str) -> Callable[[Workflow], object]:
    def change(job: dict) -> None:
        step = _image_step(job)
        assert find in step["run"], f"{IMAGE_STEP!r} no longer holds {find!r}"
        step["run"] = step["run"].replace(find, replace)

    return sweep_job(change)


def _move_image_step_after_the_list(job: dict) -> None:
    step = _image_step(job)
    job["steps"].remove(step)
    job["steps"].append(step)


PYTEST_SWEEP_RUNS_THE_IMAGE_TESTS = Census(
    name="pytest-sweep-runs-the-image-tests",
    reason=(
        "`addopts` deselects the `image` marker from every pytest run, and `image-leg.yml` "
        "runs it only on a pull request whose diff matches a hand-kept `paths:` list, so "
        "the sweep is the one run that reaches every image test. It selects `-m image` over "
        "the whole tree with no path list to drift, on one process so the image pulls do "
        "not race. It runs after the suite even when the suite is red, and appends to "
        "`sweep.log` before the step that lists the failed modules for the sweep issue."
    ),
    files=lambda: tracked(SWEEP),
    offence=judge(_image_step_problems),
    red=mutants(
        SWEEP,
        a_sweep_with_no_image_step=sweep_job(
            lambda job: job["steps"].remove(_image_step(job))
        ),
        an_image_step_without_the_marker=_edit_image_run(" -m image", ""),
        an_image_step_on_xdist_workers=_edit_image_run(" -n 0", ""),
        an_image_step_over_a_path_list=_edit_image_run(
            "-m image", "-m image" + IMAGE_LEG_PATHS
        ),
        an_image_step_skipped_after_a_red_suite=sweep_job(
            lambda job: _image_step(job).pop("if")
        ),
        an_image_step_that_overwrites_the_log=_edit_image_run("tee -a", "tee"),
        an_image_step_after_the_list_step=sweep_job(_move_image_step_after_the_list),
    ),
    green=(Subject(SWEEP),),
    must_find=frozenset({SWEEP}),
)
