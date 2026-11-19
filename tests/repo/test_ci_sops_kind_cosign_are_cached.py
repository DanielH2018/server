"""Every inline cosign fetch in CI restores the release from a cache keyed on its pin.

A GitHub release-host 504 fails any job that fetches a binary uncached with a short retry
window, so each fetch is cached.
Each fetch takes the shape `test_ci_flux_cli_is_cached.py` holds the flux CLI to: an
`actions/cache` step keyed on the pin, a curl only on a miss with nfpm's `--retry-all-errors`
window, and a `sha256sum -c` against the pin on every run.
sops and kind, and cosign in the drills, come through `.github/actions/pinned-tool`, which
tests/repo/test_release_fetches_use_pinned_tool.py holds to the same shape and which also
refuses an installer action. This module names the jobs whose cosign fetch stays inline.

The jobs are named rather than derived, because the fix removes the strings a derivation would
match.

Run: uv run pytest tests/repo/test_ci_sops_kind_cosign_are_cached.py
"""

import json
import re
import sys

import pytest
from _helpers import REPO
from lib import yaml_fast

CACHE = "actions/cache@"
PIN_EXPRESSION = re.compile(r"\$\{\{[^}]*\b(?:env\.|steps\.|inputs\.|hashFiles\()")
RETRY = re.compile(r"--retry (\d+)\b.*--retry-delay (\d+)\b")
SHA256 = re.compile(r"\b[0-9a-f]{64}\b")
RELEASE = "sigstore/cosign/releases/download"
PINNED = re.compile(r"\$RUNNER_TEMP/pinned/cosign[\w-]*")
# Every job that fetches cosign inline.
FETCHES = (
    ("promote.yml", "publish"),
    ("promote.yml", "promote"),
    ("prune-artifacts.yml", "prune"),
)


def _load(path: str) -> dict:
    return yaml_fast.safe_load((REPO / path).read_text(encoding="utf-8")) or {}


def _normalise(text: str) -> str:
    return text.replace("${{ runner.temp }}", "$RUNNER_TEMP").replace(
        "${RUNNER_TEMP}", "$RUNNER_TEMP"
    )


def _job(workflow: str, job: str) -> tuple[dict, list[dict]]:
    loaded = _load(f".github/workflows/{workflow}")
    return loaded, ((loaded.get("jobs") or {}).get(job) or {}).get("steps") or []


def _step_text(step: dict) -> str:
    return _normalise(str(step.get("run", "")) + json.dumps(step.get("env") or {}))


def _installs(steps: list[dict]) -> list[tuple[int, dict]]:
    """The steps that fetch or install the cosign release, with their positions."""
    return [
        (i, s)
        for i, s in enumerate(steps)
        if RELEASE in _step_text(s) or PINNED.search(_step_text(s))
    ]


def _cache_paths(step: dict) -> list[str]:
    raw = _normalise(str((step.get("with") or {}).get("path", "")))
    return [
        line.strip().removesuffix("/*")
        for line in raw.splitlines()
        if line.strip() and not line.strip().startswith("!")
    ]


def _curls(text: str) -> list[str]:
    return [
        line.strip() for line in text.splitlines() if line.strip().startswith("curl ")
    ]


def _unguarded_curls(text: str) -> list[str]:
    """Curl lines not inside the `else` of an `if [[ -f ... ]]` cache check."""
    found, stack = [], []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("if "):
            stack.append(["-f" in stripped, False])
        elif stripped == "else" and stack:
            stack[-1][1] = True
        elif stripped == "fi" and stack:
            stack.pop()
        elif stripped.startswith("curl ") and not (
            stack and stack[-1][0] and stack[-1][1]
        ):
            found.append(stripped)
    return found


def _pinned_tool_retry_window() -> int:
    """The pinned-tool action's retries times delay, the window a release outage needs."""
    steps = (_load(".github/actions/pinned-tool/action.yml").get("runs") or {}).get(
        "steps"
    )
    windows = [
        int(m[1]) * int(m[2])
        for s in steps or []
        for line in _curls(str(s.get("run", "")))
        if (m := RETRY.search(line))
    ]
    assert windows, "the pinned-tool action no longer curls with a retry window"
    return max(windows)


def _cosign_pins(workflow: str, job: str) -> set[str]:
    """The sha256 digests in the job's cosign install steps and its `COSIGN*` env keys."""
    loaded, steps = _job(workflow, job)
    installs = [s for _, s in _installs(steps)]
    envs = [loaded.get("env"), ((loaded.get("jobs") or {}).get(job) or {}).get("env")]
    envs += [s.get("env") for s in installs]
    values = [str(v) for e in envs for k, v in (e or {}).items() if "COSIGN" in k]
    text = "\n".join(values + [str(s.get("run", "")) for s in installs])
    return set(SHA256.findall(text))


@pytest.mark.parametrize(("workflow", "job"), FETCHES)
def test_each_job_restores_the_tool_from_a_cache_keyed_on_the_pin(workflow, job):
    _, steps = _job(workflow, job)
    installs = _installs(steps)
    assert installs, f"{workflow} {job}: no step fetches the cosign release"
    first, step = installs[0]
    targets = set(PINNED.findall(_step_text(step)))
    assert targets, (
        f"{workflow} {job}: the cosign fetch writes outside $RUNNER_TEMP/pinned/"
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


@pytest.mark.parametrize(("workflow", "job"), FETCHES)
def test_each_job_fetches_the_tool_only_on_a_cache_miss(workflow, job):
    _, steps = _job(workflow, job)
    runs = [str(s.get("run", "")) for _, s in _installs(steps)]
    curls = [line for text in runs for line in _curls(text)]
    assert curls, f"{workflow} {job}: no step curls the cosign release"
    assert [line for text in runs for line in _unguarded_curls(text)] == [], (
        f"{workflow} {job}: a cosign curl runs outside the cache-miss branch"
    )


@pytest.mark.parametrize(("workflow", "job"), FETCHES)
def test_each_fetch_outlasts_a_release_outage(workflow, job):
    floor = _pinned_tool_retry_window()
    _, steps = _job(workflow, job)
    curls = [
        line for _, s in _installs(steps) for line in _curls(str(s.get("run", "")))
    ]
    assert curls, f"{workflow} {job}: no step curls the cosign release"
    short = [
        line
        for line in curls
        if "--retry-all-errors" not in line
        or not (m := RETRY.search(line))
        or int(m[1]) * int(m[2]) < floor
    ]
    assert short == [], (
        f"{workflow} {job}: each cosign curl must retry all errors for at least {floor}s"
    )


@pytest.mark.parametrize(("workflow", "job"), FETCHES)
def test_each_job_checks_the_pinned_sha256(workflow, job):
    _, steps = _job(workflow, job)
    installs = [s for _, s in _installs(steps)]
    assert installs, f"{workflow} {job}: no step fetches the cosign release"
    assert _cosign_pins(workflow, job), f"{workflow} {job}: no cosign sha256 pin"
    checks = [s for s in installs if "sha256sum -c" in str(s.get("run", ""))]
    assert checks, f"{workflow} {job}: no cosign step runs sha256sum -c against the pin"


def _broken(monkeypatch, workflow: str, job: str, mutate) -> None:
    """Point `_job` at a copy of one job with `mutate` applied to its steps."""
    loaded, steps = _job(workflow, job)
    mutate(steps)
    real = _job
    monkeypatch.setattr(
        sys.modules[__name__],
        "_job",
        lambda w, j: (loaded, steps) if (w, j) == (workflow, job) else real(w, j),
    )


def _edit_fetch(steps: list[dict], old: str, new: str) -> None:
    _, step = _installs(steps)[0]
    assert old in step["run"], old
    step["run"] = step["run"].replace(old, new)


def test_a_job_without_its_cache_step_is_flagged(monkeypatch):
    _broken(
        monkeypatch,
        "promote.yml",
        "promote",
        lambda steps: steps.__setitem__(
            slice(None),
            [s for s in steps if "pinned/cosign" not in str(s.get("with", ""))],
        ),
    )
    with pytest.raises(AssertionError, match="no earlier pin-keyed actions/cache"):
        test_each_job_restores_the_tool_from_a_cache_keyed_on_the_pin(
            "promote.yml", "promote"
        )


def test_a_cache_with_a_constant_key_is_flagged(monkeypatch):
    def constant_key(steps):
        for s in steps:
            if "pinned/cosign" in str(s.get("with", "")):
                s["with"]["key"] = "cosign-linux"

    _broken(monkeypatch, "promote.yml", "publish", constant_key)
    with pytest.raises(AssertionError, match="no earlier pin-keyed actions/cache"):
        test_each_job_restores_the_tool_from_a_cache_keyed_on_the_pin(
            "promote.yml", "publish"
        )


def test_a_curl_outside_the_cache_check_is_flagged(monkeypatch):
    _broken(
        monkeypatch,
        "prune-artifacts.yml",
        "prune",
        lambda steps: _edit_fetch(
            steps, 'if [[ -f "$cosign" ]]; then', 'if [[ -n "$COSIGN_VERSION" ]]; then'
        ),
    )
    with pytest.raises(AssertionError, match="outside the cache-miss branch"):
        test_each_job_fetches_the_tool_only_on_a_cache_miss(
            "prune-artifacts.yml", "prune"
        )


def test_a_curl_with_a_short_retry_window_is_flagged(monkeypatch):
    _broken(
        monkeypatch,
        "promote.yml",
        "promote",
        lambda steps: _edit_fetch(steps, "--retry 12", "--retry 5"),
    )
    with pytest.raises(AssertionError, match="must retry all errors"):
        test_each_fetch_outlasts_a_release_outage("promote.yml", "promote")


def test_a_cosign_fetch_without_its_digest_check_is_flagged(monkeypatch):
    _broken(
        monkeypatch,
        "prune-artifacts.yml",
        "prune",
        lambda steps: _edit_fetch(steps, "| sha256sum -c --strict", "> /dev/null"),
    )
    with pytest.raises(AssertionError, match="no cosign step runs sha256sum -c"):
        test_each_job_checks_the_pinned_sha256("prune-artifacts.yml", "prune")
