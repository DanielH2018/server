"""Every GitHub release download in the workflows goes through `.github/actions/pinned-tool`.

The action restores the asset from an actions/cache keyed on the tool, version and sha256,
curls the release only on a cache miss with a retry window that outlasts a release-host outage,
and checks the digest on every run. Outside it, a step that curls a release download is a
problem unless `EXEMPT` names its job and the release repo it fetches with the reason, and so is
a download from any other host, which reads as that host
(`_release_fetch_census.py` says which invocations count), and so is
a third-party installer action. Every exempt fetch is held to the action's own cache, retry and
digest shape. A script a step runs is out of scope: the `a-downloading-script-runs-after-its-cache`
row of the workflow-row table holds its downloads to a cache.

Run: uv run pytest tests/repo/ci_workflows/test_release_fetches_use_pinned_tool.py
"""

import copy
import re

import pytest
from _helpers import REPO
from _release_fetch_census import UNRESOLVED, release_fetches
from homelab_tool_fetch import TOOL_RETRY_WINDOW_S
from lib import yaml_fast

ACTION = ".github/actions/pinned-tool/action.yml"
USES = "$/.github/actions/pinned-tool"
FETCH = re.compile(r"(?:^|[\s;&|(`])(?:curl|wget)\s|\bgh release download\b", re.M)
RETRY = re.compile(r"--retry (\d+)\b.*--retry-delay (\d+)\b")
INSTALLER = re.compile(r"setup-|install|/action@|-action@")

NO_CHECKOUT = (
    "checks out nothing (promote.yml's job table), so no local action reaches it"
)
GITLEAKS = (
    "gitleaks checks against the release's checksums.txt, so its version has one home in "
    "prek.toml"
)
FLUX = "fluxcd/flux2"
COSIGN = "sigstore/cosign"
GITLEAKS_REPO = "gitleaks/gitleaks"
# `test_every_exemption_still_fetches` fails on a row whose job no longer fetches that repo.
EXEMPT = {
    ("promote.yml", "publish", FLUX): NO_CHECKOUT,
    ("promote.yml", "publish", COSIGN): NO_CHECKOUT,
    ("promote.yml", "tag", FLUX): NO_CHECKOUT,
    ("promote.yml", "settle", FLUX): NO_CHECKOUT,
    ("promote.yml", "reconcile", FLUX): NO_CHECKOUT,
    ("promote.yml", "reset-move", FLUX): NO_CHECKOUT,
    ("ci.yml", "host_packages_build", GITLEAKS_REPO): GITLEAKS,
    ("promote.yml", "render", GITLEAKS_REPO): GITLEAKS,
}
# release repo -> the tool name an inline fetch's `pinned/<tool>` path and `<TOOL>_*` pins use.
INLINE_TOOLS = {FLUX: "flux", COSIGN: "cosign", GITLEAKS_REPO: "gitleaks"}
# file -> the pin the gitleaks cache key reads: prek.toml's rev in ci.yml, the URL in promote.yml.
GITLEAKS_PINS = {
    "ci.yml": "steps.gitleaks.outputs.version",
    "promote.yml": "env.GITLEAKS_URL",
}
GITLEAKS_DIGEST = r"sha256sum -c --ignore-missing --strict checksums\.txt"
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
    ("ci.yml", "hooks", "vale"),
    ("bootstrap-drill.yml", "drill", "flux"),
    ("bootstrap-drill.yml", "drill", "sops"),
    ("bootstrap-drill.yml", "drill", "kind"),
    ("flux-deletion-drill.yml", "drill", "flux"),
    ("flux-deletion-drill.yml", "drill", "cosign"),
    ("flux-deletion-drill.yml", "drill", "kind"),
    ("pytest-tools", "pytest-tools", "sops"),
    ("promote.yml", "promote", "cosign"),
    ("prune-artifacts.yml", "prune", "cosign"),
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


def fetch_problems(
    steps: list[dict],
    pins: tuple[str, ...] = ("inputs.tool", "inputs.version", "inputs.sha256"),
    digest: str = r"\$SHA256 .*\| sha256sum -c --strict",
) -> list[str]:
    """What a cache step and the fetch step after it lack of a cached, retried, checked fetch.

    Args:
        steps: The steps of one tool's fetch, the action's by default.
        pins: The expressions the cache key must read.
        digest: The pattern of the line that checks the download's sha256.
    """
    found = []
    cache = next(
        (s for s in steps if str(s.get("uses", "")).startswith("actions/cache@")), {}
    )
    key = str((cache.get("with") or {}).get("key", ""))
    for pin in pins:
        if pin not in key:
            found.append(f"the cache key does not read {pin}")
    fetch = next((s for s in steps if FETCH.search(str(s.get("run", "")))), {})
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
    if not re.search(digest, run):
        found.append("the fetch does not check the download's sha256")
    return found


def inline_problems(workflow: dict, file: str, job: str, repo: str) -> list[str]:
    """What a job's inline fetch from `repo` lacks of the action's cache, retry and digest check."""
    tool = INLINE_TOOLS[repo]
    steps = [
        s
        for s in workflow["jobs"][job]["steps"]
        if f"pinned/{tool}" in str(s.get("run", "")) + str(s.get("with", ""))
    ]
    if repo == GITLEAKS_REPO:
        return fetch_problems(steps, (GITLEAKS_PINS[file],), GITLEAKS_DIGEST)
    pin = tool.upper()
    return fetch_problems(
        steps,
        (f"env.{pin}_VERSION", f"env.{pin}_SHA256"),
        rf"\${pin}_SHA256 .*\| sha256sum -c --strict",
    )


def _action_tools() -> set[str]:
    run = next(str(s["run"]) for s in _action_steps() if "run" in s)
    return set(re.findall(r"^\s*([a-z0-9-]+)\)$", run, re.M))


def test_the_action_fetches_from_a_pinned_cache_with_a_retried_digest_checked_curl():
    assert fetch_problems(_action_steps()) == []


@pytest.mark.parametrize(("file", "job", "repo"), sorted(EXEMPT))
def test_an_exempt_inline_fetch_has_the_actions_shape(file, job, repo):
    assert inline_problems(_sources()[file], file, job, repo) == []


def test_the_census_finds_the_known_callers():
    assert KNOWN_CALLERS <= callers(_sources())


def unknown_tools(
    sources: dict[str, dict], tools: set[str]
) -> set[tuple[str, str, str]]:
    """The callers naming a tool outside `tools`, which the action's `case` would exit 1 on."""
    return {c for c in callers(sources) if c[2] not in tools}


def test_every_caller_names_a_tool_the_action_fetches():
    tools = _action_tools()
    assert {c[2] for c in KNOWN_CALLERS} <= tools, f"the action's case reads as {tools}"
    unknown = unknown_tools(_sources(), tools)
    assert unknown == set(), f"pinned-tool has no release URL for {sorted(unknown)}"


def test_a_caller_naming_an_unknown_tool_is_flagged():
    sources = copy.deepcopy(_sources())
    sources["ci.yml"]["jobs"]["flux_roundtrip"]["steps"].append(
        {"uses": USES, "with": {"tool": "helm"}}
    )
    assert unknown_tools(sources, _action_tools()) == {
        ("ci.yml", "flux_roundtrip", "helm")
    }


def test_no_release_fetch_runs_outside_the_action():
    found = release_fetches(_sources()) - set(EXEMPT)
    assert found == set(), f"fetch these through {USES}: {sorted(found)}"


def test_every_exemption_still_fetches():
    assert set(EXEMPT) - release_fetches(_sources()) == set()


def test_no_unlisted_installer_action_runs():
    assert installer_uses(_sources()) - set(INSTALLERS) == set()


NFPM_URL = "https://github.com/goreleaser/nfpm/releases/download/fixture/nfpm.tar.gz"


NFPM = "goreleaser/nfpm"


@pytest.mark.parametrize(
    ("run", "repo"),
    [
        (f"curl -fsSLo f {NFPM_URL}", NFPM),
        (f"out=$(curl -fsSL {NFPM_URL})", NFPM),
        (f"mkdir -p d && curl -fsSLo d/f {NFPM_URL}", NFPM),
        (f"wget -q {NFPM_URL}", NFPM),
        ("gh release download v1 -R goreleaser/nfpm -p 'nfpm_*'", NFPM),
        (
            "curl -fsSLo f https://github.com/goreleaser/nfpm/releases/latest/download/n",
            NFPM,
        ),
        (
            'repo=goreleaser/nfpm; curl -fsSLo f "https://github.com/$repo/releases/download/fixture/n"',
            UNRESOLVED,
        ),
        (
            'curl -fsSLo f "https://github.com/${OWNER}/nfpm/releases/download/fixture/n"',
            UNRESOLVED,
        ),
        (
            'curl -fsSLo f "https://github.com/${{ env.REPO }}/releases/download/fixture/n"',
            UNRESOLVED,
        ),
        ('gh release download v1 --repo "${{ env.REPO }}" -p x', UNRESOLVED),
        ('gh release download v1 -R "$REPO" -p x', UNRESOLVED),
        ("gh release download v1 -p x", UNRESOLVED),
        (f"curl -fsSL {NFPM_URL} -o tool 2>/dev/null", NFPM),
    ],
)
def test_an_inline_fetch_beside_an_exempt_one_is_flagged(run, repo):
    sources = copy.deepcopy(_sources())
    sources["ci.yml"]["jobs"]["host_packages_build"]["steps"].append(
        {"run": run + "\n"}
    )
    assert release_fetches(sources) - set(EXEMPT) == {
        ("ci.yml", "host_packages_build", repo)
    }


@pytest.mark.parametrize(
    ("run", "host"),
    [
        (
            "curl -fsSLo /tmp/helm.tgz https://get.helm.sh/helm-v3.16.0.tgz",
            "get.helm.sh",
        ),
        ("curl -fsSL -O https://dl.k8s.io/release/v1.31.0/bin/kubectl", "dl.k8s.io"),
        (
            "curl -fsSL --output a.zip https://github.com/o/r/archive/refs/tags/v1.zip",
            "github.com",
        ),
        (
            "curl -fsSL \\\n  -o f https://objects.githubusercontent.com/x",
            "objects.githubusercontent.com",
        ),
        ("wget -q https://get.helm.sh/helm.tgz", "get.helm.sh"),
        ('curl -fsSLo f "$URL"', UNRESOLVED),
        (
            "curl -fsSL https://get.helm.sh/h.tgz | tar -xz -C /usr/local/bin",
            "get.helm.sh",
        ),
        ("curl -fsSL https://get.helm.sh/h.tgz > /tmp/h.tgz", "get.helm.sh"),
        ("timeout 60 curl -fsSLo /tmp/k https://dl.k8s.io/k", "dl.k8s.io"),
        ("if curl -fsSLo /tmp/k https://dl.k8s.io/k; then true; fi", "dl.k8s.io"),
        ('u=https://dl.k8s.io/k\ncurl -fsSLo f "$u"', "dl.k8s.io"),
        ("curl -fsSL -o f https://get.helm.sh/h.tgz 2> /dev/null", "get.helm.sh"),
        ("curl -fsSLo f https://get.helm.sh/h.tgz > /dev/null", "get.helm.sh"),
        (
            "curl -fsSL https://get.helm.sh/h.tgz | tar -xz > /dev/null",
            "get.helm.sh",
        ),
        (
            "curl -fsSLo f https://get.helm.sh/h.tgz; tar -xz f > /dev/null",
            "get.helm.sh",
        ),
    ],
)
def test_a_download_from_another_host_is_flagged(run, host):
    sources = copy.deepcopy(_sources())
    sources["ci.yml"]["jobs"]["host_packages_build"]["steps"].append(
        {"run": run + "\n"}
    )
    assert release_fetches(sources) - set(EXEMPT) == {
        ("ci.yml", "host_packages_build", host)
    }


@pytest.mark.parametrize(
    "run",
    [
        'printf \'%s\' "$auth" | curl -fsS -K - -o "$body" https://ghcr.io/v2/x',
        "curl -fsS http://127.0.0.1:5010/v2/ > /dev/null 2>&1 && break",
        "curl -fsS -o /dev/null http://127.0.0.1:5010/v2/",
        "curl -fsS --output /dev/null https://get.helm.sh/h.tgz 2>&1",
        "wget -q -O /dev/null https://get.helm.sh/h.tgz",
        "curl -fsS http://127.0.0.1:5010/v2/ 1>/dev/null",
        "curl -fsS http://127.0.0.1:5010/v2/ &>/dev/null",
    ],
)
def test_a_curl_that_downloads_nothing_is_not_a_fetch(run):
    sources = copy.deepcopy(_sources())
    sources["ci.yml"]["jobs"]["host_packages_build"]["steps"].append(
        {"run": run + "\n"}
    )
    assert release_fetches(sources) - set(EXEMPT) == set()


@pytest.mark.parametrize(
    "steps",
    [
        [{"run": "curl -fsSLo /tmp/k https://dl.k8s.io/k\n"}],
        [
            {
                "run": 'curl -fsSLo a "${GITLEAKS_URL}checksums.txt"\n'
                "curl -fsSLo /tmp/k https://dl.k8s.io/k\n"
            }
        ],
    ],
    ids=["own step", "beside the gitleaks curl"],
)
def test_a_download_in_a_job_whose_env_names_a_release_is_flagged(steps):
    sources = copy.deepcopy(_sources())
    sources["promote.yml"]["jobs"]["render"]["steps"].extend(steps)
    assert release_fetches(sources) - set(EXEMPT) == {
        ("promote.yml", "render", "dl.k8s.io")
    }


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
    assert any(problem in p for p in fetch_problems(steps)), fetch_problems(steps)


@pytest.mark.parametrize(
    ("old", "new", "problem"),
    [
        (
            "-${{ env.COSIGN_SHA256 }}",
            "",
            "the cache key does not read env.COSIGN_SHA256",
        ),
        ("}}/pinned/cosign\n", "}}/elsewhere\n", "no actions/cache step runs before"),
        ('if [[ -f "$cosign" ]]; then', "if true; then", "outside the cache-miss"),
        ("--retry 12", "--retry 3", "curl retries for under"),
        ("--retry-all-errors ", "", "curl retries only some errors"),
        ("| sha256sum -c --strict", "> /dev/null", "does not check the download"),
    ],
)
def test_a_weakened_inline_fetch_is_flagged(old, new, problem):
    text = (REPO / ".github/workflows/promote.yml").read_text(encoding="utf-8")
    assert old in text, old
    found = inline_problems(
        yaml_fast.safe_load(text.replace(old, new)), "promote.yml", "publish", COSIGN
    )
    assert any(problem in p for p in found), found


@pytest.mark.parametrize(
    ("old", "new", "problem"),
    [
        (
            "-${{ steps.gitleaks.outputs.version }}",
            "",
            "the cache key does not read steps.gitleaks.outputs.version",
        ),
        ("}}/pinned/gitleaks\n", "}}/elsewhere\n", "no actions/cache step runs before"),
        (
            'if [[ -f "$dl/$archive" && -f "$dl/checksums.txt" ]]; then',
            "if true; then",
            "outside the cache-miss",
        ),
        ("--retry 12", "--retry 3", "curl retries for under"),
        ("--ignore-missing --strict checksums.txt", "--version", "does not check"),
    ],
)
def test_a_weakened_gitleaks_fetch_is_flagged(old, new, problem):
    text = (REPO / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert old in text, old
    found = inline_problems(
        yaml_fast.safe_load(text.replace(old, new)),
        "ci.yml",
        "host_packages_build",
        GITLEAKS_REPO,
    )
    assert any(problem in p for p in found), found
