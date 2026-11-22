"""The census row holding CI's cluster-bundle scan to promote's render and gitleaks steps."""

from _ci_workflow_rows import ci_row
from _promote_render_workflow import build_script
from _row_table import Census
from _workflow_rows import (
    CI,
    PROMOTE,
    Workflow,
    jobs,
    judge,
    load,
    mutants,
    runs,
    steps,
)

BUILD_JOB = "host_packages_build"
BUNDLE = "bundle"
BUNDLE_SCAN = "bundle-gitleaks"
SCAN_ARGS = " dir out "


def _step(wf: Workflow, job: str, step_id: str) -> dict:
    return next((s for s in steps(jobs(wf).get(job, {})) if s.get("id") == step_id), {})


def _scan_args(script: str) -> str:
    """The arguments after the gitleaks binary on the line that scans `out`, or ""."""
    line = next((ln for ln in script.splitlines() if SCAN_ARGS in ln), "")
    return line[line.find(SCAN_ARGS) :].strip()


def _bundle_problems(wf: Workflow) -> list[str]:
    promote = load(PROMOTE)
    found = []
    if runs(_step(wf, BUILD_JOB, BUNDLE)) != build_script(promote):
        found.append(f"step `{BUNDLE}` does not run promote's `build` script verbatim")
    want = _scan_args(runs(_step(promote, "render", "gitleaks")))
    got = _scan_args(runs(_step(wf, BUILD_JOB, BUNDLE_SCAN)))
    if not want:
        found.append("promote's `gitleaks` step has no `dir out` scan line")
    elif got != want:
        found.append(f"step `{BUNDLE_SCAN}` scans with {got!r}, promote with {want!r}")
    return found


def _edit_run(step_id: str, old: str, new: str):
    def mutate(wf: Workflow) -> None:
        step = _step(wf, BUILD_JOB, step_id)
        step["run"] = runs(step).replace(old, new)

    return mutate


BUNDLE_SCAN_MATCHES_PROMOTE = Census(
    **ci_row(
        name="host-package-bundle-scan-matches-promote",
        reason=(
            "The renderer drops YAML comments, so a template line's `# gitleaks:allow` passes the "
            "prek hook and trips promote's bundle scan after the merge. `host_packages_build` "
            "scans the same bundle on every PR, and it catches that line only while it renders "
            "with promote's build script and scans with promote's gitleaks arguments."
        ),
        offence=judge(_bundle_problems),
        red=mutants(
            CI,
            a_render_without_the_published_tags=_edit_run(
                BUNDLE, '"${roles[@]}"', "--shared-only"
            ),
            a_scan_with_another_config=_edit_run(
                BUNDLE_SCAN, "--config .gitleaks.toml", "--config other.toml"
            ),
            a_scan_of_another_tree=_edit_run(
                BUNDLE_SCAN, " dir out ", " dir out/cluster "
            ),
            no_scan_step=lambda wf: steps(jobs(wf)[BUILD_JOB]).remove(
                _step(wf, BUILD_JOB, BUNDLE_SCAN)
            ),
        ),
    )
)
