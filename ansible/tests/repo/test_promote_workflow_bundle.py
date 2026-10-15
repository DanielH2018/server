"""The `promote` workflow's render job builds the bundle the renderer would, and censuses it.

With no published tag the build step writes the bundle itself, because `cluster` without
`--role` renders every tag (D2.6b). Its two literals, D1's empty root and the empty bundle's
`digests.json`, must equal the renderer's own output for no tag, byte for byte. Otherwise the
empty bundle's `digests.json` would differ from the one a render of the same tree writes. The
census and gates step, `promote.py check-bundle`, must run after the build and before
gitleaks, and both before the step that hands `out/` to `publish`, the job that pushes. The
build passes no `--secret-stubs`, which is what selects placeholder mode (C1). The promote
job fetches master's whole history, because `decide` reads `Removes-Object` lines back to
`prod`'s commit. `test_promote_workflow.py` holds the workflow's other properties, and its
docstring says why each is a static test.

Run: uv run pytest ansible/tests/repo/test_promote_workflow_bundle.py
"""

import copy

import pytest
from _helpers import REPO
from lib import yaml_fast
from renderer.renderer_lib.bundle import digests, digests_text, kustomization_text

WORKFLOW = REPO / ".github" / "workflows" / "promote.yml"
CENSUS = "promote.py check-bundle out"


def _render_steps(wf: dict) -> list[dict]:
    return wf["jobs"]["render"]["steps"]


def _index(wf: dict, step_id: str) -> int | None:
    return next(
        (i for i, s in enumerate(_render_steps(wf)) if s.get("id") == step_id), None
    )


def _handoff(wf: dict) -> int | None:
    """The index of the step that uploads `out/` for `publish`."""
    return next(
        (
            i
            for i, s in enumerate(_render_steps(wf))
            if str(s.get("uses", "")).startswith("actions/upload-artifact@")
        ),
        None,
    )


def problems(wf: dict) -> list[str]:
    """Every broken property of the render job's bundle steps, as one line each."""
    steps = _render_steps(wf)
    build, check, gitleaks = (
        _index(wf, "build"),
        _index(wf, "check-bundle"),
        _index(wf, "gitleaks"),
    )
    found = []
    if check is None or CENSUS not in str(steps[check].get("run", "")):
        found.append(f"render: runs no `{CENSUS}` step with id `check-bundle`")
    elif build is None or gitleaks is None or not build < check < gitleaks:
        found.append(
            "render: does not run check-bundle after the build and before gitleaks"
        )
    handoff = _handoff(wf)
    if handoff is None or wf["jobs"].get("publish", {}).get("needs") != "render":
        found.append("render: hands out/ to no publish job that needs it")
    elif any(i is None or i > handoff for i in (check, gitleaks)):
        found.append("render: hands out/ on before check-bundle or gitleaks runs")
    script = str(steps[build].get("run", "")) if build is not None else ""
    if "--secret-stubs" in script:
        found.append("render: the build step passes --secret-stubs, a stub-mode render")
    promote = (wf["jobs"].get("promote") or {}).get("steps") or []
    depths = [
        (s.get("with") or {}).get("fetch-depth")
        for s in promote
        if str(s.get("uses", "")).startswith("actions/checkout@")
    ]
    if depths != [0]:
        found.append(
            "promote: does not fetch the history decide reads trailers from "
            f"(fetch-depth {depths})"
        )
    root = [f"'{line}'" for line in kustomization_text([]).splitlines()]
    if not all(line in script for line in root):
        found.append("render: the build step's empty root is not the renderer's")
    if f"'{digests_text(digests({}, [])).rstrip()}'" not in script:
        found.append(
            "render: the build step's empty digests.json is not the renderer's"
        )
    return found


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml_fast.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_the_render_job_builds_and_censuses_the_bundle_is_clean(workflow):
    assert problems(workflow) == []


def _census_after_gitleaks(wf):
    steps = _render_steps(wf)
    check = steps.pop(_index(wf, "check-bundle") or 0)
    steps.insert((_index(wf, "gitleaks") or 0) + 1, check)


def _without_the_census(wf):
    steps = _render_steps(wf)
    steps[:] = [s for s in steps if s.get("id") != "check-bundle"]


def _build_script(wf, old, new):
    step = _render_steps(wf)[_index(wf, "build") or 0]
    step["run"] = step["run"].replace(old, new)


def _gitleaks_after_handoff(wf):
    steps = _render_steps(wf)
    scan = steps.pop(_index(wf, "gitleaks") or 0)
    steps.append(scan)


def _shallow_promote(wf):
    for step in wf["jobs"]["promote"]["steps"]:
        if str(step.get("uses", "")).startswith("actions/checkout@"):
            step["with"]["fetch-depth"] = 1


RED = {
    "the census after gitleaks": (
        _census_after_gitleaks,
        "render: does not run check-bundle after the build",
    ),
    "no census step": (_without_the_census, "render: runs no `promote.py"),
    "gitleaks after the handoff": (
        _gitleaks_after_handoff,
        "render: hands out/ on before check-bundle or gitleaks runs",
    ),
    "a publish job that does not need render": (
        lambda wf: wf["jobs"]["publish"].pop("needs"),
        "render: hands out/ to no publish job that needs it",
    ),
    "a stub-mode build": (
        lambda wf: _build_script(wf, "--out out", "--out out --secret-stubs s.yml"),
        "render: the build step passes --secret-stubs",
    ),
    "a shallow promote checkout": (
        _shallow_promote,
        "promote: does not fetch the history decide reads trailers from",
    ),
    "an empty digests.json that drifted": (
        lambda wf: _build_script(wf, '"paths": []', '"paths": ["x"]'),
        "the build step's empty digests.json is not the renderer's",
    ),
    "an empty root that drifted": (
        lambda wf: _build_script(wf, "'resources: []'", "'resources: [x]'"),
        "the build step's empty root is not the renderer's",
    ),
}


@pytest.mark.parametrize("case", RED, ids=list(RED))
def test_each_broken_bundle_step_is_flagged(workflow, case):
    mutate, expected = RED[case]
    broken = copy.deepcopy(workflow)
    mutate(broken)
    found = problems(broken)
    assert any(expected in line for line in found), found
