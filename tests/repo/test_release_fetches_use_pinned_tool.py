"""Every GitHub release download in the workflows goes through `.github/actions/pinned-tool`.

The action restores the asset from an actions/cache keyed on the tool, version and sha256,
curls the release only on a cache miss with a retry window that outlasts a release-host outage,
and checks the digest on every run. Outside it, a step that curls a release download is a
problem unless `EXEMPT` names its job with the reason, and so is a third-party installer action.

Run: uv run pytest tests/repo/test_release_fetches_use_pinned_tool.py
"""

import copy
import re

import pytest
from _helpers import REPO
from homelab_tool_fetch import TOOL_RETRY_WINDOW_S
from lib import yaml_fast

ACTION = ".github/actions/pinned-tool/action.yml"
USES = "$/.github/actions/pinned-tool"
RELEASE = re.compile(r"github\.com/[\w.-]+/[\w.-]+/releases/download\b")
CURL = re.compile(r"^\s*curl\s", re.M)
RETRY = re.compile(r"--retry (\d+)\b.*--retry-delay (\d+)\b")
INSTALLER = re.compile(r"setup-|install|/action@|-action@")

NO_CHECKOUT = (
    "checks out nothing (promote.yml's job table), so no local action reaches it"
)
# (file, job) -> why its inline release curl stays. `test_every_exemption_still_curls` fails
# on a row whose job no longer curls.
EXEMPT = {
    ("promote.yml", "publish"): NO_CHECKOUT,
    ("promote.yml", "tag"): NO_CHECKOUT,
    ("promote.yml", "settle"): NO_CHECKOUT,
    ("promote.yml", "reconcile"): NO_CHECKOUT,
    ("promote.yml", "reset-move"): NO_CHECKOUT,
    ("ci.yml", "hooks"): "Vale has no sha256 pin for the action to check (#9225)",
    ("ci.yml", "host_packages_build"): (
        "gitleaks checks against the release's checksums.txt, so its version has one home in "
        "prek.toml"
    ),
    ("promote.yml", "render"): "gitleaks, as in ci.yml's host_packages_build",
    ("promote.yml", "promote"): "moves to the action in #9222",
    ("bootstrap-drill.yml", "drill"): "moves to the action in #9219",
    ("flux-deletion-drill.yml", "drill"): "moves to the action in #9220",
    ("prune-artifacts.yml", "prune"): "moves to the action in #9221",
    ("pytest-tools", "pytest-tools"): "moves to the action in #9223",
}
# installer action -> why it stays.
INSTALLERS = {
    "astral-sh/setup-uv": "fetches uv into its own cache keyed on uv.lock",
    "actions/setup-python": "reads the runner's toolcache, then GitHub's versions manifest",
    "actions/setup-node": "reads the runner's toolcache, then nodejs.org's index",
}
# (file, job, tool) the census must find, so a reshaped workflow cannot pass empty.
KNOWN_CALLERS = {
    ("ci.yml", "flux_roundtrip", "flux"),
    ("ci.yml", "sops_manifest_type_proof", "flux"),
    ("ci.yml", "sops_manifest_type_proof", "sops"),
    ("ci.yml", "sops_manifest_type_proof", "kind"),
    ("ci.yml", "host_packages_build", "nfpm"),
}


def _sources() -> dict[str, dict]:
    """Each workflow, and each composite action but pinned-tool read as a one-job workflow."""
    found = {
        p.name: yaml_fast.safe_load(p.read_text(encoding="utf-8"))
        for p in sorted((REPO / ".github/workflows").glob("*.yml"))
    }
    for action in sorted((REPO / ".github/actions").glob("*/action.yml")):
        if action != REPO / ACTION:
            steps = yaml_fast.safe_load(action.read_text(encoding="utf-8"))["runs"]
            name = action.parent.name
            found[name] = {"jobs": {name: {"steps": steps["steps"]}}}
    return found


def _action_steps() -> list[dict]:
    return yaml_fast.safe_load((REPO / ACTION).read_text(encoding="utf-8"))["runs"][
        "steps"
    ]


def release_curls(sources: dict[str, dict]) -> set[tuple[str, str]]:
    """Every (file, job) with a step outside pinned-tool that curls a release download."""
    found = set()
    for name, workflow in sources.items():
        for job_name, job in (workflow.get("jobs") or {}).items():
            context = str(workflow.get("env") or "") + str(job.get("env") or "")
            for step in job.get("steps") or []:
                run = str(step.get("run", ""))
                if CURL.search(run) and RELEASE.search(
                    context + run + str(step.get("env") or "")
                ):
                    found.add((name, job_name))
    return found


def installer_uses(sources: dict[str, dict]) -> set[str]:
    """Every third-party installer action a step runs, by name without its ref."""
    return {
        uses.split("@")[0]
        for workflow in sources.values()
        for job in (workflow.get("jobs") or {}).values()
        for step in job.get("steps") or []
        if INSTALLER.search(uses := str(step.get("uses", "")))
        and not uses.startswith(("$/", "./"))
    }


def callers(sources: dict[str, dict]) -> set[tuple[str, str, str]]:
    """Every (file, job, tool) whose step runs pinned-tool."""
    return {
        (name, job_name, str((step.get("with") or {}).get("tool")))
        for name, workflow in sources.items()
        for job_name, job in (workflow.get("jobs") or {}).items()
        for step in job.get("steps") or []
        if step.get("uses") == USES
    }


def action_problems(steps: list[dict]) -> list[str]:
    """What the action's steps lack of a cached, retried and digest-checked fetch."""
    found = []
    cache = next(
        (s for s in steps if str(s.get("uses", "")).startswith("actions/cache@")), {}
    )
    key = str((cache.get("with") or {}).get("key", ""))
    for pin in ("inputs.tool", "inputs.version", "inputs.sha256"):
        if pin not in key:
            found.append(f"the cache key does not read {pin}")
    fetch = next((s for s in steps if CURL.search(str(s.get("run", "")))), {})
    run = str(fetch.get("run", ""))
    if not (cache and fetch) or steps.index(cache) > steps.index(fetch):
        found.append("no actions/cache step runs before the fetch")
    guard, miss = False, False
    for line in run.splitlines():
        text = line.strip()
        if text.startswith("if ") and "-f " in text:
            guard = True
        elif text == "else" and guard:
            miss = True
        elif text == "fi":
            guard = miss = False
        elif text.startswith("curl "):
            if not miss:
                found.append(f"curls outside the cache-miss branch: {text}")
            m = RETRY.search(text)
            if "--retry-all-errors" not in text or not m:
                found.append(f"curl retries only some errors: {text}")
            elif int(m[1]) * int(m[2]) < TOOL_RETRY_WINDOW_S:
                found.append(f"curl retries for under {TOOL_RETRY_WINDOW_S}s: {text}")
    if not re.search(r"\$SHA256 .*\| sha256sum -c --strict", run):
        found.append("the fetch does not check the download against inputs.sha256")
    return found


def _action_tools() -> set[str]:
    run = next(str(s["run"]) for s in _action_steps() if "run" in s)
    return set(re.findall(r"^\s*([a-z0-9-]+)\)$", run, re.M))


def test_the_action_fetches_from_a_pinned_cache_with_a_retried_digest_checked_curl():
    assert action_problems(_action_steps()) == []


def test_the_census_finds_the_known_callers():
    assert KNOWN_CALLERS <= callers(_sources())


def test_every_caller_names_a_tool_the_action_fetches():
    unknown = {c for c in callers(_sources()) if c[2] not in _action_tools()}
    assert unknown == set(), f"pinned-tool has no release URL for {sorted(unknown)}"


def test_no_release_curl_runs_outside_the_action():
    found = release_curls(_sources()) - set(EXEMPT)
    assert found == set(), f"fetch these through {USES}: {sorted(found)}"


def test_every_exemption_still_curls():
    assert set(EXEMPT) - release_curls(_sources()) == set()


def test_no_unlisted_installer_action_runs():
    assert installer_uses(_sources()) - set(INSTALLERS) == set()


def test_a_release_curl_in_a_migrated_job_is_flagged():
    sources = copy.deepcopy(_sources())
    sources["ci.yml"]["jobs"]["flux_roundtrip"]["steps"].append(
        {
            "run": "curl -fsSLo f https://github.com/fluxcd/flux2/releases/download/v1/f\n"
        }
    )
    assert ("ci.yml", "flux_roundtrip") in release_curls(sources) - set(EXEMPT)


def test_an_installer_action_is_flagged():
    sources = copy.deepcopy(_sources())
    sources["ci.yml"]["jobs"]["flux_roundtrip"]["steps"].append(
        {"uses": "sigstore/cosign-installer@v4.0.0"}
    )
    assert installer_uses(sources) - set(INSTALLERS) == {"sigstore/cosign-installer"}


@pytest.mark.parametrize(
    ("old", "new", "problem"),
    [
        ("-${{ inputs.sha256 }}", "", "the cache key does not read inputs.sha256"),
        ('if [[ -f "$download" ]]; then', "if true; then", "outside the cache-miss"),
        ("--retry 12", "--retry 3", "curl retries for under"),
        ("--retry-all-errors ", "", "curl retries only some errors"),
        ("| sha256sum -c --strict", "> /dev/null", "does not check the download"),
    ],
)
def test_a_weakened_action_is_flagged(old, new, problem):
    text = (REPO / ACTION).read_text(encoding="utf-8")
    assert old in text, old
    steps = yaml_fast.safe_load(text.replace(old, new))["runs"]["steps"]
    assert any(problem in p for p in action_problems(steps)), action_problems(steps)
