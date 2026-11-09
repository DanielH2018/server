"""Every CI job that installs the flux CLI restores it from a cache keyed on its pin (#7778).

On 2026-11-09 master run 42567649763 failed in `fluxcd/flux2/action`, which fetched the CLI
from a GitHub release on every run and gave up after five 504s. The action's own retry window
is fixed at five tries 5s apart, so a job holds the CLI the way `host_packages_build` holds
nfpm: an `actions/cache` step keyed on the pinned version and sha256, then a step that curls
the release only when the cached file is absent and checks the digest every time.

The jobs are named rather than derived from the action, because the fix removes the string a
derivation would match. A step that runs a local composite action is read as that action's
steps, so a shared install action passes.

Run: uv run pytest ansible/tests/repo/test_ci_flux_cli_is_cached.py
"""

import json
import re

import pytest
from _helpers import REPO
from lib import yaml_fast
from lib.proc_testing import run

ACTION = "fluxcd/flux2/action@"
CACHE = "actions/cache@"
FLUX_RELEASE = "fluxcd/flux2/releases/download"
PINNED_FLUX = re.compile(r"\$RUNNER_TEMP/pinned/flux[\w-]*")
PIN_EXPRESSION = re.compile(r"\$\{\{[^}]*\b(?:env\.|steps\.|inputs\.|hashFiles\()")
RETRY = re.compile(r"--retry (\d+)\b.*--retry-delay (\d+)\b")
FLUX_JOBS = (
    ("ci.yml", "flux_roundtrip"),
    ("ci.yml", "sops_manifest_type_proof"),
    ("promote.yml", "publish"),
    ("promote.yml", "tag"),
    ("promote.yml", "settle"),
    ("promote.yml", "reset-move"),
    ("bootstrap-drill.yml", "drill"),
    ("flux-deletion-drill.yml", "drill"),
)


def _load(path: str) -> dict:
    return yaml_fast.safe_load((REPO / path).read_text(encoding="utf-8")) or {}


def _normalise(text: str) -> str:
    return text.replace("${{ runner.temp }}", "$RUNNER_TEMP").replace(
        "${RUNNER_TEMP}", "$RUNNER_TEMP"
    )


def _steps(job: dict) -> list[dict]:
    """The job's steps, with each local composite action replaced by its own steps."""
    found = []
    for step in job.get("steps") or []:
        uses = str(step.get("uses", ""))
        action = REPO / uses.removeprefix("./") / "action.yml"
        if uses.startswith("./") and action.is_file():
            inner = (_load(str(action.relative_to(REPO))).get("runs") or {}).get(
                "steps"
            )
            for sub in inner or []:
                found.append({**sub, "with_caller": step.get("with") or {}})
        else:
            found.append(step)
    return found


def _job(workflow: str, job: str) -> tuple[dict, list[dict]]:
    loaded = _load(f".github/workflows/{workflow}")
    return loaded, _steps((loaded.get("jobs") or {}).get(job) or {})


def _step_text(step: dict) -> str:
    return _normalise(str(step.get("run", "")) + json.dumps(step.get("env") or {}))


def _installs(steps: list[dict]) -> list[tuple[int, dict]]:
    """The steps that fetch or unpack the flux release, with their positions."""
    return [
        (i, s)
        for i, s in enumerate(steps)
        if FLUX_RELEASE in _step_text(s) or PINNED_FLUX.search(_step_text(s))
    ]


def _cache_paths(step: dict) -> list[str]:
    raw = _normalise(str((step.get("with") or {}).get("path", "")))
    return [
        line.strip().removesuffix("/*")
        for line in raw.splitlines()
        if line.strip() and not line.strip().startswith("!")
    ]


def _curls(run: str) -> list[str]:
    return [
        line.strip() for line in run.splitlines() if line.strip().startswith("curl ")
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
        elif text.startswith("curl ") and not (stack and stack[-1][0] and stack[-1][1]):
            found.append(text)
    return found


def _nfpm_retry_window() -> int:
    """The nfpm fetch's retries times delay, the window #7655 set for a release outage."""
    _, steps = _job("ci.yml", "host_packages_build")
    windows = [
        int(m[1]) * int(m[2])
        for s in steps
        if "pinned/nfpm" in str(s.get("run", ""))
        for line in _curls(str(s.get("run", "")))
        if (m := RETRY.search(line))
    ]
    assert windows, "host_packages_build no longer curls nfpm with a retry window"
    return max(windows)


def _flux_amd64_pin() -> dict:
    tools = _load("hosts/packages/homelab-tools/package.yml").get("tools") or {}
    return (tools.get("flux") or {}).get("amd64") or {}


def test_no_workflow_installs_flux_through_the_action():
    tracked = run(
        ["git", "ls-files", ".github/workflows/*.yml", ".github/actions/*/action.yml"],
        cwd=REPO,
        check=True,
    ).stdout.split()
    assert ".github/workflows/ci.yml" in tracked
    offenders = [p for p in tracked if ACTION in (REPO / p).read_text(encoding="utf-8")]
    assert offenders == [], (
        "fluxcd/flux2/action downloads the CLI uncached with a fixed 5x5s retry window"
    )


@pytest.mark.parametrize(("workflow", "job"), FLUX_JOBS)
def test_each_flux_job_restores_the_cli_from_a_cache_keyed_on_the_pin(workflow, job):
    _, steps = _job(workflow, job)
    installs = _installs(steps)
    assert installs, f"{workflow} {job}: no step fetches the flux release"
    first, step = installs[0]
    targets = set(PINNED_FLUX.findall(_step_text(step)))
    assert targets, (
        f"{workflow} {job}: the flux fetch writes outside $RUNNER_TEMP/pinned/"
    )
    caches = {
        path: str((s.get("with") or {}).get("key", ""))
        for s in steps[:first]
        if str(s.get("uses", "")).startswith(CACHE)
        for path in _cache_paths(s)
    }
    keyed = {p for p, key in caches.items() if PIN_EXPRESSION.search(key)}
    assert any(t == p or t.startswith(p + "/") for t in targets for p in keyed), (
        f"{workflow} {job}: no earlier pin-keyed actions/cache step caches {sorted(targets)}"
    )


@pytest.mark.parametrize(("workflow", "job"), FLUX_JOBS)
def test_each_flux_job_fetches_only_on_a_cache_miss(workflow, job):
    _, steps = _job(workflow, job)
    runs = [str(s.get("run", "")) for _, s in _installs(steps)]
    curls = [line for run in runs for line in _curls(run)]
    assert curls, f"{workflow} {job}: no step curls the flux release"
    assert [line for run in runs for line in _unguarded_curls(run)] == []


@pytest.mark.parametrize(("workflow", "job"), FLUX_JOBS)
def test_each_flux_fetch_outlasts_a_release_outage(workflow, job):
    floor = _nfpm_retry_window()
    _, steps = _job(workflow, job)
    curls = [
        line for _, s in _installs(steps) for line in _curls(str(s.get("run", "")))
    ]
    assert curls, f"{workflow} {job}: no step curls the flux release"
    short = [
        line
        for line in curls
        if "--retry-all-errors" not in line
        or not (m := RETRY.search(line))
        or int(m[1]) * int(m[2]) < floor
    ]
    assert short == [], f"each flux curl must retry all errors for at least {floor}s"


@pytest.mark.parametrize(("workflow", "job"), FLUX_JOBS)
def test_each_flux_job_checks_the_pinned_sha256(workflow, job):
    pin = _flux_amd64_pin()
    assert pin.get("sha256"), "homelab-tools no longer pins the amd64 flux digest"
    loaded, steps = _job(workflow, job)
    context = json.dumps([loaded.get("env"), loaded.get("jobs", {}).get(job), steps])
    assert pin["sha256"] in context, f"{workflow} {job}: the flux sha256 is not pinned"
    assert str(pin.get("version")) in context, f"{workflow} {job}: no flux version pin"
    checks = [s for _, s in _installs(steps) if "sha256sum -c" in str(s.get("run", ""))]
    assert checks, f"{workflow} {job}: no flux step runs sha256sum -c against the pin"
