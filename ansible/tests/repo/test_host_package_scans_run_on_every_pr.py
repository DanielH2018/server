"""The `host_packages` job's two package-root scans run on every PR (H4.1c).

The job's later steps stand down when no rendered tree differs. The gitleaks scan and the
placeholder check read the head render's package roots, and a PR that changes only
`.gitleaks.toml` or the gitleaks pin renders nothing differently, so a scan behind that gate would
skip on exactly the PRs that change what it enforces. Each scan therefore sits after the render
step, carries no `if:`, and comes before the first step that tests the render's `changed` output.

Run: uv run pytest ansible/tests/repo/test_host_package_scans_run_on_every_pr.py
"""

from _helpers import REPO
from lib import yaml_fast

SCANS = {
    "Scan the named package roots for secrets": "scan-roots",
    "Check the named package roots' placeholders against their SOPS keys": "placeholders",
}
GATE = "steps.render.outputs.changed"


def _job_steps() -> list[dict]:
    workflow = yaml_fast.safe_load((REPO / ".github/workflows/ci.yml").read_text())
    return workflow["jobs"]["host_packages"]["steps"]


def scan_problems(steps: list[dict]) -> list[str]:
    """What keeps a scan from running on every PR, or the scan step from being found."""
    names = [step.get("name") for step in steps]
    ids = [step.get("id") for step in steps]
    if "render" not in ids:
        return ["the job has no step with id `render`"]
    render = ids.index("render")
    gated = [i for i, step in enumerate(steps) if GATE in str(step.get("if", ""))]
    problems = []
    for name, command in SCANS.items():
        if name not in names:
            problems.append(f"no step is named {name!r}")
            continue
        index = names.index(name)
        step = steps[index]
        if index < render:
            problems.append(f"{name!r} runs before the render")
        if "if" in step:
            problems.append(f"{name!r} has an `if:`, so it can skip")
        if gated and index > gated[0]:
            problems.append(f"{name!r} comes after a step gated on {GATE}")
        if command not in str(step.get("run", "")):
            problems.append(f"{name!r} does not run `{command}`")
    return problems


def _steps(*specs: tuple[str, dict]) -> list[dict]:
    return [{"name": name, **fields} for name, fields in specs]


def _good() -> list[dict]:
    return _steps(
        ("Render", {"id": "render", "run": "x"}),
        *((name, {"run": f"uv run {cmd}"}) for name, cmd in SCANS.items()),
        ("Build", {"if": f"{GATE} == 'true'", "run": "x"}),
    )


def test_the_live_job_runs_both_scans_on_every_pr():
    assert scan_problems(_job_steps()) == []


def test_scans_after_the_render_and_ahead_of_the_gate_are_clean():
    assert scan_problems(_good()) == []


def test_a_gated_scan_is_flagged():
    steps = _good()
    steps[1]["if"] = f"{GATE} == 'true'"
    assert (
        "'Scan the named package roots for secrets' has an `if:`, so it can skip"
        in scan_problems(steps)
    )


def test_a_scan_placed_after_the_gate_is_flagged():
    steps = _good()
    steps.append(steps.pop(1))
    assert any("after a step gated" in p for p in scan_problems(steps))


def test_a_scan_before_the_render_is_flagged():
    steps = _good()
    steps.insert(0, steps.pop(1))
    assert any("before the render" in p for p in scan_problems(steps))


def test_a_missing_scan_step_is_flagged_by_name():
    steps = [s for s in _good() if s["name"] != next(iter(SCANS))]
    assert scan_problems(steps) == [f"no step is named {next(iter(SCANS))!r}"]
