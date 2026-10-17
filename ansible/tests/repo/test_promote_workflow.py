"""The `promote` workflow keeps render code away from every token that can sign or move a tag.

The `prune-artifacts` workflow, which deletes old versions of the same artifact, is held in
`test_prune_artifacts_workflow.py`, which imports this module's readers.

WHY THIS IS A TEST. `promote.yml` triggers on `workflow_run`, so GitHub runs master's copy of
the file and a PR that edits it never runs its own edit. Every property below is therefore
checked statically, before the edit reaches master. The plan behind each one is
docs/target-architecture/deploy/d1-flux-installed.md (*Decisions*, and *Steps, as PRs*, D1.4).

`problems` reads the parsed workflow and name every broken property. The
real files must produce none, and each red fixture is a real file with one property broken,
which must produce the problem it names.

Run: uv run pytest ansible/tests/repo/test_promote_workflow.py
"""

import copy

import pytest
from _helpers import REPO
from lib import yaml_fast

WORKFLOW = REPO / ".github" / "workflows" / "promote.yml"
FLUX_DEFAULTS = REPO / "ansible" / "roles" / "k8s" / "flux" / "defaults" / "main.yml"

JOBS = ("render", "images", "publish", "promote", "tag", "verify", "settle", "post")
NEEDS = {
    "publish": ["render", "images"],
    "promote": "publish",
    "tag": "promote",
    "verify": ["promote", "tag"],
    "settle": ["promote", "tag", "verify"],
    "post": ["promote", "tag", "verify", "settle"],
}

# Every job runs only for a successful push run of CI on master in this repository.
TRIGGER_GUARDS = (
    "github.event.workflow_run.conclusion == 'success'",
    "github.event.workflow_run.event == 'push'",
    "github.event.workflow_run.head_branch == 'master'",
    "github.event.workflow_run.head_repository.full_name == github.repository",
)
# A dispatch reaches every job, so every job also requires master's copy of the file.
REF_GUARD = "github.ref == 'refs/heads/master'"
PERMISSIONS = {
    "render": {"contents": "read", "checks": "read"},
    "images": {"contents": "read", "packages": "write"},
    "publish": {"packages": "write", "id-token": "write"},
    "promote": {
        "contents": "read",
        "checks": "read",
        "packages": "read",
        "pull-requests": "read",
        "statuses": "read",
    },
    "tag": {"packages": "write"},
    "verify": {"contents": "read", "statuses": "read"},
    "settle": {"packages": "write", "actions": "write"},
    "post": {"contents": "read"},
}

RENDER_REF = "${{ inputs.sha || github.event.workflow_run.head_sha || github.sha }}"
# The ancestry step must read the dispatched commit, never the workflow's own `github.sha`.
DISPATCHED_SHA = "inputs.sha"
PROMOTE_REF = "${{ github.sha }}"
COSIGN_RELEASE = "v3.1.3"
HEAD_SHA = "workflow_run.head_sha"
# `tag` moves `prod` only when `decide` said so.
VERDICT_GATE = "needs.promote.outputs.verdict == 'promote'"


def _load(path) -> dict:
    return yaml_fast.safe_load(path.read_text(encoding="utf-8"))


def _triggers(wf: dict) -> dict:
    # YAML 1.1 reads the bare key `on` as the boolean True.
    return wf.get("on", wf.get(True)) or {}


def _steps(job: dict) -> list[dict]:
    return job.get("steps") or []


def _uses(step: dict, action: str) -> bool:
    return str(step.get("uses", "")).split("@")[0] == action


def _checkouts(job: dict) -> list[dict]:
    return [s for s in _steps(job) if _uses(s, "actions/checkout")]


def _step_index(job: dict, step_id: str) -> int | None:
    return next((i for i, s in enumerate(_steps(job)) if s.get("id") == step_id), None)


def _guard_problems(name: str, job: dict) -> list[str]:
    condition = " ".join(str(job.get("if", "")).split())
    found = [
        f"{name}: its `if` lacks the trigger guard {guard}"
        for guard in TRIGGER_GUARDS
        if guard not in condition
    ]
    if REF_GUARD not in condition:
        found.append(f"{name}: its `if` lacks the master-ref guard {REF_GUARD}")
    return found


def _checkout_problems(jobs: dict) -> list[str]:
    found = []
    for name, ref in (
        ("render", RENDER_REF),
        ("promote", PROMOTE_REF),
        ("verify", PROMOTE_REF),
        ("post", PROMOTE_REF),
    ):
        checkouts = _checkouts(jobs.get(name, {}))
        if len(checkouts) != 1:
            found.append(f"{name}: has {len(checkouts)} checkout steps, not one")
            continue
        args = checkouts[0].get("with") or {}
        if args.get("ref") != ref:
            found.append(f"{name}: checks out {args.get('ref')!r}, not {ref!r}")
        # `verify` and `post` read no history: what they read came from `decide`.
        if name not in ("verify", "post") and args.get("fetch-depth") != 0:
            found.append(f"{name}: checks out without fetch-depth: 0")
    for name in ("publish", "tag", "settle"):
        if _checkouts(jobs.get(name, {})):
            found.append(f"{name}: checks out the repository")
    return found


def _ancestry_problems(render: dict) -> list[str]:
    ancestry, build = _step_index(render, "ancestry"), _step_index(render, "build")
    recheck = _step_index(render, "ci-recheck")
    if build is None:
        return ["render: has no step with id `build`"]
    if ancestry is None or "git merge-base --is-ancestor" not in str(
        _steps(render)[ancestry].get("run", "")
    ):
        return [
            "render: runs no `git merge-base --is-ancestor` step with id `ancestry`"
        ]
    found = []
    if ancestry > build:
        found.append("render: checks ancestry after the build step")
    if DISPATCHED_SHA not in str(_steps(render)[ancestry].get("env")):
        found.append(f"render: the ancestry step does not read {DISPATCHED_SHA}")
    if recheck is None or "promote.py ci-verdict" not in str(
        _steps(render)[recheck].get("run", "")
    ):
        found.append(
            "render: runs no `promote.py ci-verdict` step with id `ci-recheck`"
        )
    elif recheck > build:
        found.append("render: re-checks a dispatched commit's CI after the build step")
    return found


def _handoff_problems(jobs: dict) -> list[str]:
    found = []
    promote, tag = jobs.get("promote", {}), jobs.get("tag", {})
    decide = _step_index(promote, "decide")
    if decide is None:
        return ["promote: has no step with id `decide`"]
    for key in ("target", "digest"):
        if (
            promote.get("outputs", {}).get(key)
            != f"${{{{ steps.decide.outputs.{key} }}}}"
        ):
            found.append(f"promote: output {key} does not come from the decide step")
        if f"needs.promote.outputs.{key}" not in str(tag.get("steps")):
            found.append(f"tag: does not take {key} from promote's outputs")
    later = [jobs.get(name, {}) for name in ("tag", "verify", "settle", "post")]
    after_decide = _steps(promote)[decide + 1 :] + [s for j in later for s in _steps(j)]
    if any(HEAD_SHA in str(step) for step in after_decide) or any(
        HEAD_SHA in str(job.get("if", "")) for job in later
    ):
        found.append(f"a step after decide reads github.event.{HEAD_SHA}")
    # Without the gate a `held` or `current` verdict reaches `tag`, which then fails red on
    # the empty target instead of skipping, and the hold rests on a shell check alone.
    if VERDICT_GATE not in " ".join(str(tag.get("if", "")).split()):
        found.append(f"tag: its `if` lacks the verdict gate {VERDICT_GATE}")
    return found


def _pin_problems(jobs: dict, flux_version: str) -> list[str]:
    found = []
    flux_steps = [
        (name, s)
        for name, job in jobs.items()
        for s in _steps(job)
        if _uses(s, "fluxcd/flux2/action")
    ]
    if {name for name, _ in flux_steps} != {"publish", "tag", "settle"}:
        found.append("the flux CLI is not installed in exactly publish, tag and settle")
    for name, step in flux_steps:
        version = (step.get("with") or {}).get("version")
        if version != flux_version:
            found.append(
                f"{name}: flux CLI {version!r} differs from flux_version {flux_version!r}"
            )
    cosign_steps = [
        (name, s)
        for name, job in jobs.items()
        for s in _steps(job)
        if _uses(s, "sigstore/cosign-installer")
    ]
    if {name for name, _ in cosign_steps} != {"publish", "promote"}:
        found.append("cosign is not installed in exactly publish and promote")
    for name, step in cosign_steps:
        release = (step.get("with") or {}).get("cosign-release")
        if release != COSIGN_RELEASE:
            found.append(f"{name}: cosign-installer sets cosign-release {release!r}")
    return found


def problems(wf: dict, flux_version: str) -> list[str]:
    """Every property of the workflow that is broken, as one line each."""
    found = []
    triggers = _triggers(wf)
    run = triggers.get("workflow_run") or {}
    if run.get("workflows") != ["CI"] or run.get("types") != ["completed"]:
        found.append("the workflow_run trigger is not CI's completed runs")
    for banned in ("pull_request", "pull_request_target"):
        if banned in triggers:
            found.append(f"the workflow triggers on {banned}")
    if wf.get("permissions") != {}:
        found.append("the workflow grants permissions above the job level")

    jobs = wf.get("jobs") or {}
    if set(jobs) != set(JOBS):
        found.append(f"the jobs are {sorted(jobs)}, not {sorted(JOBS)}")
    for name in JOBS:
        job = jobs.get(name, {})
        found += _guard_problems(name, job)
        if job.get("permissions") != PERMISSIONS[name]:
            found.append(
                f"{name}: permissions are {job.get('permissions')}, not {PERMISSIONS[name]}"
            )
        if name in NEEDS and job.get("needs") != NEEDS[name]:
            found.append(f"{name}: needs {job.get('needs')!r}, not {NEEDS[name]!r}")
    for name in ("promote", "tag"):
        group = jobs.get(name, {}).get("concurrency")
        if group != {"group": "promote", "cancel-in-progress": False}:
            found.append(f"{name}: is not in the promote concurrency group")

    found += _checkout_problems(jobs)
    found += _ancestry_problems(jobs.get("render", {}))
    found += _handoff_problems(jobs)
    found += _pin_problems(jobs, flux_version)
    return found


@pytest.fixture(scope="module")
def workflow() -> dict:
    return _load(WORKFLOW)


@pytest.fixture(scope="module")
def flux_version() -> str:
    return _load(FLUX_DEFAULTS)["flux_version"]


def test_the_promote_workflow_holds_every_property(workflow, flux_version):
    assert problems(workflow, flux_version) == []


def _drop_guard(wf):
    job = wf["jobs"]["render"]
    job["if"] = job["if"].replace(TRIGGER_GUARDS[2], "true")


def _drop_ref_guard(wf):
    job = wf["jobs"]["tag"]
    job["if"] = job["if"].replace(REF_GUARD, "true")


def _extra_permission(wf):
    wf["jobs"]["publish"]["permissions"]["contents"] = "read"


def _drop_needs(wf):
    del wf["jobs"]["tag"]["needs"]


def _tag_reads_head_sha(wf):
    step = next(s for s in wf["jobs"]["tag"]["steps"] if s.get("name") == "Move prod")
    step["env"]["TARGET"] = "${{ github.event.workflow_run.head_sha }}"


def _promote_checks_out_head_sha(wf):
    _checkouts(wf["jobs"]["promote"])[0]["with"]["ref"] = RENDER_REF


def _cosign_without_release(wf):
    step = next(
        s
        for s in wf["jobs"]["publish"]["steps"]
        if _uses(s, "sigstore/cosign-installer")
    )
    del step["with"]["cosign-release"]


def _render_without_ancestry(wf):
    steps = wf["jobs"]["render"]["steps"]
    steps[:] = [s for s in steps if s.get("id") != "ancestry"]


def _publish_checks_out(wf):
    checkout = copy.deepcopy(_checkouts(wf["jobs"]["render"])[0])
    wf["jobs"]["publish"]["steps"].insert(0, checkout)


def _tag_outside_the_group(wf):
    del wf["jobs"]["tag"]["concurrency"]


def _flux_version_drift(wf):
    step = next(
        s for s in wf["jobs"]["tag"]["steps"] if _uses(s, "fluxcd/flux2/action")
    )
    step["with"]["version"] = "v0.0.1"


def _pull_request_trigger(wf):
    _triggers(wf)["pull_request"] = None


def _tag_without_the_verdict_gate(wf):
    job = wf["jobs"]["tag"]
    job["if"] = job["if"].replace(VERDICT_GATE, "true")


RED = {
    "a trigger guard removed": (
        _drop_guard,
        "render: its `if` lacks the trigger guard",
    ),
    "a job without the ref check": (
        _drop_ref_guard,
        "tag: its `if` lacks the master-ref guard",
    ),
    "an extra permission": (_extra_permission, "publish: permissions are"),
    "tag without needs": (_drop_needs, "tag: needs None"),
    "tag reads head_sha": (_tag_reads_head_sha, "a step after decide reads"),
    "promote checks out head_sha": (
        _promote_checks_out_head_sha,
        "promote: checks out",
    ),
    "cosign without its release": (
        _cosign_without_release,
        "publish: cosign-installer sets",
    ),
    "render without the ancestry check": (_render_without_ancestry, "render: runs no"),
    "publish checks out": (_publish_checks_out, "publish: checks out the repository"),
    "tag outside the promote group": (
        _tag_outside_the_group,
        "tag: is not in the promote",
    ),
    "a drifted flux CLI": (_flux_version_drift, "tag: flux CLI 'v0.0.1' differs"),
    "a pull_request trigger": (
        _pull_request_trigger,
        "the workflow triggers on pull_request",
    ),
    "tag without the verdict gate": (
        _tag_without_the_verdict_gate,
        "tag: its `if` lacks the verdict gate",
    ),
}


@pytest.mark.parametrize("case", RED, ids=list(RED))
def test_each_broken_property_is_named(workflow, flux_version, case):
    """The red halves: the real file with one property broken must fail with that property."""
    mutate, expected = RED[case]
    broken = copy.deepcopy(workflow)
    mutate(broken)
    found = problems(broken, flux_version)
    assert any(expected in line for line in found), found
