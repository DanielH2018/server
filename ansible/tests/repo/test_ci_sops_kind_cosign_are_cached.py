"""Every CI job that fetches sops, kind or cosign restores it from a cache keyed on its pin (#7781).

On 2026-11-09 PR #7780's CI run failed three jobs on GitHub release-host 504s: the bootstrap
drill's `Install sops, age and kind` step curled both binaries uncached with a 5x5s retry
window, and the flux-deletion drill's `sigstore/cosign-installer` fetched cosign the same way.
Each fetch takes the shape `test_ci_flux_cli_is_cached.py` holds the flux CLI to: an
`actions/cache` step keyed on the pin, a curl only on a miss with nfpm's `--retry-all-errors`
window, and a `sha256sum -c` against the pin on every run. cosign leaves its installer action
for a pinned, cached fetch of the release binary.

The jobs are named rather than derived, because the fix removes the strings a derivation would
match. A job whose fetch already holds a property is left out of that property's set.

Run: uv run pytest ansible/tests/repo/test_ci_sops_kind_cosign_are_cached.py
"""

import json
import re
import sys

import pytest
from _helpers import REPO
from lib import yaml_fast
from lib.proc_testing import run

COSIGN_ACTION = "sigstore/cosign-installer@"
CACHE = "actions/cache@"
PIN_EXPRESSION = re.compile(r"\$\{\{[^}]*\b(?:env\.|steps\.|inputs\.|hashFiles\()")
RETRY = re.compile(r"--retry (\d+)\b.*--retry-delay (\d+)\b")
SHA256 = re.compile(r"\b[0-9a-f]{64}\b")
RELEASES = {
    "sops": "getsops/sops/releases/download",
    "kind": "kubernetes-sigs/kind/releases/download",
    "cosign": "sigstore/cosign/releases/download",
}
# Every job that fetches each tool.
FETCHES = (
    ("sops", "ci.yml", "pytest"),
    ("sops", "ci.yml", "sops_manifest_type_proof"),
    ("sops", "bootstrap-drill.yml", "drill"),
    ("kind", "ci.yml", "sops_manifest_type_proof"),
    ("kind", "bootstrap-drill.yml", "drill"),
    ("kind", "flux-deletion-drill.yml", "drill"),
    ("cosign", "flux-deletion-drill.yml", "drill"),
    ("cosign", "promote.yml", "publish"),
    ("cosign", "promote.yml", "promote"),
    ("cosign", "prune-artifacts.yml", "prune"),
)
# ci.yml's two jobs cached sops and kind under #7693.
UNCACHED = tuple(f for f in FETCHES if f[1] != "ci.yml")
# The drills already check sops and kind against their pins.
UNCHECKED = tuple(f for f in FETCHES if f[0] == "cosign")


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


def _pinned(tool: str) -> re.Pattern[str]:
    return re.compile(rf"\$RUNNER_TEMP/pinned/{tool}[\w-]*")


def _installs(tool: str, steps: list[dict]) -> list[tuple[int, dict]]:
    """The steps that fetch or install the tool's release, with their positions."""
    return [
        (i, s)
        for i, s in enumerate(steps)
        if RELEASES[tool] in _step_text(s) or _pinned(tool).search(_step_text(s))
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


def _cosign_pins(workflow: str, job: str) -> set[str]:
    """The sha256 digests in the job's cosign install steps and its `COSIGN*` env keys."""
    loaded, steps = _job(workflow, job)
    installs = [s for _, s in _installs("cosign", steps)]
    envs = [loaded.get("env"), ((loaded.get("jobs") or {}).get(job) or {}).get("env")]
    envs += [s.get("env") for s in installs]
    values = [str(v) for e in envs for k, v in (e or {}).items() if "COSIGN" in k]
    text = "\n".join(values + [str(s.get("run", "")) for s in installs])
    return set(SHA256.findall(text))


def test_no_workflow_installs_cosign_through_the_action():
    tracked = run(
        ["git", "ls-files", ".github/workflows/*.yml", ".github/actions/*/action.yml"],
        cwd=REPO,
        check=True,
    ).stdout.split()
    assert ".github/workflows/promote.yml" in tracked
    offenders = [
        p for p in tracked if COSIGN_ACTION in (REPO / p).read_text(encoding="utf-8")
    ]
    assert offenders == [], (
        "sigstore/cosign-installer downloads cosign uncached with a fixed retry window"
    )


@pytest.mark.parametrize(("tool", "workflow", "job"), UNCACHED)
def test_each_job_restores_the_tool_from_a_cache_keyed_on_the_pin(tool, workflow, job):
    _, steps = _job(workflow, job)
    installs = _installs(tool, steps)
    assert installs, f"{workflow} {job}: no step fetches the {tool} release"
    first, step = installs[0]
    targets = set(_pinned(tool).findall(_step_text(step)))
    assert targets, (
        f"{workflow} {job}: the {tool} fetch writes outside $RUNNER_TEMP/pinned/"
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


@pytest.mark.parametrize(("tool", "workflow", "job"), UNCACHED)
def test_each_job_fetches_the_tool_only_on_a_cache_miss(tool, workflow, job):
    _, steps = _job(workflow, job)
    runs = [str(s.get("run", "")) for _, s in _installs(tool, steps)]
    curls = [line for text in runs for line in _curls(text)]
    assert curls, f"{workflow} {job}: no step curls the {tool} release"
    assert [line for text in runs for line in _unguarded_curls(text)] == [], (
        f"{workflow} {job}: a {tool} curl runs outside the cache-miss branch"
    )


@pytest.mark.parametrize(("tool", "workflow", "job"), FETCHES)
def test_each_fetch_outlasts_a_release_outage(tool, workflow, job):
    floor = _nfpm_retry_window()
    _, steps = _job(workflow, job)
    curls = [
        line
        for _, s in _installs(tool, steps)
        for line in _curls(str(s.get("run", "")))
    ]
    assert curls, f"{workflow} {job}: no step curls the {tool} release"
    short = [
        line
        for line in curls
        if "--retry-all-errors" not in line
        or not (m := RETRY.search(line))
        or int(m[1]) * int(m[2]) < floor
    ]
    assert short == [], (
        f"{workflow} {job}: each {tool} curl must retry all errors for at least {floor}s"
    )


@pytest.mark.parametrize(("tool", "workflow", "job"), UNCHECKED)
def test_each_job_checks_the_pinned_sha256(tool, workflow, job):
    _, steps = _job(workflow, job)
    installs = [s for _, s in _installs(tool, steps)]
    assert installs, f"{workflow} {job}: no step fetches the {tool} release"
    assert _cosign_pins(workflow, job), f"{workflow} {job}: no {tool} sha256 pin"
    checks = [s for s in installs if "sha256sum -c" in str(s.get("run", ""))]
    assert checks, f"{workflow} {job}: no {tool} step runs sha256sum -c against the pin"


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


def _edit_fetch(tool: str, steps: list[dict], old: str, new: str) -> None:
    _, step = _installs(tool, steps)[0]
    assert old in step["run"], old
    step["run"] = step["run"].replace(old, new)


def test_a_job_without_its_cache_step_is_flagged(monkeypatch):
    _broken(
        monkeypatch,
        "bootstrap-drill.yml",
        "drill",
        lambda steps: steps.__setitem__(
            slice(None),
            [s for s in steps if "pinned/sops" not in str(s.get("with", ""))],
        ),
    )
    with pytest.raises(AssertionError, match="no earlier pin-keyed actions/cache"):
        test_each_job_restores_the_tool_from_a_cache_keyed_on_the_pin(
            "sops", "bootstrap-drill.yml", "drill"
        )


def test_a_cache_with_a_constant_key_is_flagged(monkeypatch):
    def constant_key(steps):
        for s in steps:
            if "pinned/cosign" in str(s.get("with", "")):
                s["with"]["key"] = "cosign-linux"

    _broken(monkeypatch, "promote.yml", "publish", constant_key)
    with pytest.raises(AssertionError, match="no earlier pin-keyed actions/cache"):
        test_each_job_restores_the_tool_from_a_cache_keyed_on_the_pin(
            "cosign", "promote.yml", "publish"
        )


def test_a_curl_outside_the_cache_check_is_flagged(monkeypatch):
    _broken(
        monkeypatch,
        "flux-deletion-drill.yml",
        "drill",
        lambda steps: _edit_fetch(
            "kind",
            steps,
            'if [[ -f "$kind" ]]; then',
            'if [[ -n "$KIND_VERSION" ]]; then',
        ),
    )
    with pytest.raises(AssertionError, match="outside the cache-miss branch"):
        test_each_job_fetches_the_tool_only_on_a_cache_miss(
            "kind", "flux-deletion-drill.yml", "drill"
        )


def test_a_curl_with_a_short_retry_window_is_flagged(monkeypatch):
    _broken(
        monkeypatch,
        "ci.yml",
        "pytest",
        lambda steps: _edit_fetch("sops", steps, "--retry 12", "--retry 5"),
    )
    with pytest.raises(AssertionError, match="must retry all errors"):
        test_each_fetch_outlasts_a_release_outage("sops", "ci.yml", "pytest")


def test_a_cosign_fetch_without_its_digest_check_is_flagged(monkeypatch):
    _broken(
        monkeypatch,
        "prune-artifacts.yml",
        "prune",
        lambda steps: _edit_fetch(
            "cosign", steps, "| sha256sum -c --strict", "> /dev/null"
        ),
    )
    with pytest.raises(AssertionError, match="no cosign step runs sha256sum -c"):
        test_each_job_checks_the_pinned_sha256("cosign", "prune-artifacts.yml", "prune")


def test_a_workflow_using_the_cosign_action_is_flagged(monkeypatch):
    path = REPO / ".github/workflows/promote.yml"
    text = path.read_text(encoding="utf-8")
    real = type(path).read_text

    def with_action(self, *args, **kwargs):
        if self == path:
            return text + f"\n# uses: {COSIGN_ACTION}0000\n"
        return real(self, *args, **kwargs)

    monkeypatch.setattr(type(path), "read_text", with_action)
    with pytest.raises(AssertionError, match="cosign-installer downloads cosign"):
        test_no_workflow_installs_cosign_through_the_action()
