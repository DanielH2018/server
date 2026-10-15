"""The `promote` workflow's render job builds the bundle the renderer would, and censuses it.

With no published tag the build step calls `cluster --shared-only`, because `cluster`
without `--role` renders every tag. That bundle holds the shared kinds and a root listing them
(D3.3a). The census and gates step, `promote.py check-bundle`, must run after the build and
before gitleaks. So must the step that runs `cluster_vars.py check --ciphertext-only` on the
bundle's SOPS copies, the guard that stands in for the scan `.gitleaks.toml` skips there. All
three run before the step that hands `out/` to `publish`, the job that pushes. The
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

WORKFLOW = REPO / ".github" / "workflows" / "promote.yml"
CENSUS = "promote.py check-bundle out"
SHARED_ONLY = "homelab_render.py cluster --out out --shared-only"
NO_TAG = 'if [ -z "$listed" ]; then'
CIPHERTEXT = "cluster_vars.py check --ciphertext-only"


def _render_steps(wf: dict) -> list[dict]:
    return wf["jobs"]["render"]["steps"]


def _index(wf: dict, step_id: str) -> int | None:
    return next(
        (i for i, s in enumerate(_render_steps(wf)) if s.get("id") == step_id), None
    )


def _step(wf: dict, step_id: str) -> int:
    """The index of the render step `step_id`, for a mutation. Raises when it is absent, so a
    red case cannot pass by mutating nothing."""
    index = _index(wf, step_id)
    if index is None:
        raise AssertionError(
            f"render has no step with id {step_id}; the mutation is a no-op"
        )
    return index


def _no_tag_branch(script: str) -> list[str]:
    """The stripped lines of the build script's `if [ -z "$listed" ]` branch, up to its `else`."""
    _, found, rest = script.partition(NO_TAG)
    lines = []
    for line in rest.splitlines() if found else []:
        if line.strip() in ("else", "fi"):
            break
        lines.append(line.strip())
    return lines


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
    build, check, ciphertext, gitleaks = (
        _index(wf, "build"),
        _index(wf, "check-bundle"),
        _index(wf, "ciphertext"),
        _index(wf, "gitleaks"),
    )
    found = []
    if check is None or CENSUS not in str(steps[check].get("run", "")):
        found.append(f"render: runs no `{CENSUS}` step with id `check-bundle`")
    elif build is None or gitleaks is None or not build < check < gitleaks:
        found.append(
            "render: does not run check-bundle after the build and before gitleaks"
        )
    if ciphertext is None or CIPHERTEXT not in str(steps[ciphertext].get("run", "")):
        found.append(f"render: runs no `{CIPHERTEXT}` step with id `ciphertext`")
    elif build is None or gitleaks is None or not build < ciphertext < gitleaks:
        found.append(
            "render: does not check the SOPS copies after the build and before gitleaks"
        )
    handoff = _handoff(wf)
    if handoff is None or wf["jobs"].get("publish", {}).get("needs") != "render":
        found.append("render: hands out/ to no publish job that needs it")
    elif any(i is None or i > handoff for i in (check, ciphertext, gitleaks)):
        found.append(
            "render: hands out/ on before check-bundle, the ciphertext check or gitleaks "
            "runs"
        )
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
    if f"uv run python scripts/renderer/{SHARED_ONLY}" not in _no_tag_branch(script):
        found.append(
            f"render: the build step's no-tag branch does not run `{SHARED_ONLY}`"
        )
    return found


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml_fast.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_the_render_job_builds_and_censuses_the_bundle_is_clean(workflow):
    assert problems(workflow) == []


def _census_after_gitleaks(wf):
    steps = _render_steps(wf)
    check = steps.pop(_step(wf, "check-bundle"))
    steps.insert(_step(wf, "gitleaks") + 1, check)


def _without_the_census(wf):
    steps = _render_steps(wf)
    steps[:] = [s for s in steps if s.get("id") != "check-bundle"]


def _build_script(wf, old, new):
    step = _render_steps(wf)[_step(wf, "build")]
    assert old in step["run"], (
        f"the build script holds no {old!r}; the mutation is a no-op"
    )
    step["run"] = step["run"].replace(old, new)


def _ciphertext_after_gitleaks(wf):
    steps = _render_steps(wf)
    step = steps.pop(_step(wf, "ciphertext"))
    steps.insert(_step(wf, "gitleaks") + 1, step)


def _shared_only_in_the_tag_branch(wf):
    _build_script(wf, '"${roles[@]}"', '"${roles[@]}" --shared-only')
    _build_script(
        wf, "cluster --out out --shared-only\n", "cluster --out out --role x\n"
    )


def _gitleaks_after_handoff(wf):
    steps = _render_steps(wf)
    scan = steps.pop(_step(wf, "gitleaks"))
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
        "render: hands out/ on before check-bundle, the ciphertext check or gitleaks",
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
    "a no-tag branch that renders no shared kind": (
        lambda wf: _build_script(wf, " --shared-only", ""),
        "the build step's no-tag branch does not run",
    ),
    "--shared-only only in the branch that publishes tags": (
        _shared_only_in_the_tag_branch,
        "the build step's no-tag branch does not run",
    ),
    "the ciphertext check after gitleaks": (
        _ciphertext_after_gitleaks,
        "render: does not check the SOPS copies after the build and before gitleaks",
    ),
    "no ciphertext check": (
        lambda wf: _render_steps(wf).pop(_step(wf, "ciphertext")),
        "render: runs no `cluster_vars.py check --ciphertext-only` step",
    ),
}


@pytest.mark.parametrize("case", RED, ids=list(RED))
def test_each_broken_bundle_step_is_flagged(workflow, case):
    mutate, expected = RED[case]
    broken = copy.deepcopy(workflow)
    mutate(broken)
    found = problems(broken)
    assert any(expected in line for line in found), found
