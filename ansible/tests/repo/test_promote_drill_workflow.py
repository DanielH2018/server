"""A `verify_drill` dispatch of `promote.yml` pushes a bundle no signed push can carry.

WHY THIS IS A TEST. The drill is meant to make the `verify-drill` OCIRepository read
Ready=False. On 2026-10-13 it verified instead (#4820): `--reproducible` gave the drill the
same digest as the `sha-<X>` bundle `publish` had already signed, and cosign v3 stores a
signature as a referrer of the digest, so every tag on that digest verifies. The drill push
therefore carries an annotation the signed push never does, and the same dispatch proves its
own red half: `cosign verify` under promote's identity must refuse the drill digest.

These checks sit beside `test_promote_workflow.py` rather than in it because that module is
near the 500-line test-module cap. `drill_problems` names every broken property. The real
file must produce none, and each red fixture breaks one.

Run: uv run pytest ansible/tests/repo/test_promote_drill_workflow.py
"""

import copy

import forge
import pytest
from test_promote_workflow import WORKFLOW, _load, _steps

DRILL_ANNOTATION = "--annotations=io.homelab.verify-drill=true"
DRILL_IF = "${{ inputs.verify_drill }}"
SIGNED_IF = "${{ !inputs.verify_drill }}"
DRILL_DIGEST = "${{ steps.drill-push.outputs.digest }}"
# `if cosign verify ...; then exit 1` fails the run when verification succeeds.
MUST_FAIL = "if cosign verify"
# The only cosign errors that mean "the registry answered and held no signature". Any other
# failure must fail the run, or an outage reads as a refusal.
REFUSAL = "no valid bundles exist in registry|no signatures found"
DIGEST_FORM = "grep -Eqx 'sha256:[0-9a-f]{64}'"


def _step(publish: dict, step_id: str) -> dict:
    return next((s for s in _steps(publish) if s.get("id") == step_id), {})


def _refuses_problems(publish: dict) -> list[str]:
    ids = [s.get("id") for s in _steps(publish)]
    if "drill-refuses" not in ids or ids.index("drill-refuses") < ids.index(
        "drill-push"
    ):
        return ["publish: has no step `drill-refuses` after the drill push"]
    refuses = _step(publish, "drill-refuses")
    env, run = refuses.get("env") or {}, str(refuses.get("run", ""))
    found = []
    if refuses.get("if") != DRILL_IF:
        found.append(f"publish: drill-refuses runs on {refuses.get('if')!r}")
    if MUST_FAIL not in run or "cosign sign" in run:
        found.append(f"publish: drill-refuses does not fail when `{MUST_FAIL}` passes")
    if REFUSAL not in run:
        found.append("publish: drill-refuses accepts any cosign failure as a refusal")
    if DIGEST_FORM not in run:
        found.append("publish: drill-refuses does not check the digest's form")
    if env.get("DIGEST") != DRILL_DIGEST:
        found.append("publish: drill-refuses does not verify the drill push's digest")
    if (env.get("IDENTITY"), env.get("ISSUER")) != (
        forge.SIGNER_IDENTITY,
        forge.SIGNER_ISSUER,
    ):
        found.append("publish: drill-refuses verifies under an identity forge.py lacks")
    return found


def drill_problems(wf: dict) -> list[str]:
    """Every broken property of the signed push and the drill path, as one line each."""
    publish = (wf.get("jobs") or {}).get("publish", {})
    push, drill = _step(publish, "push"), _step(publish, "drill-push")
    if not push or not drill:
        return ["publish: lacks the step `push` or `drill-push`"]
    found = []
    signed_run, drill_run = str(push.get("run", "")), str(drill.get("run", ""))
    if push.get("if") != SIGNED_IF or "cosign sign" not in signed_run:
        found.append("publish: the signed push does not sign, or runs on a drill")
    if "io.homelab.verify-drill" in signed_run:
        found.append("publish: the signed push carries the drill annotation")
    if drill.get("if") != DRILL_IF or "cosign sign" in drill_run:
        found.append("publish: the drill push signs, or runs without the drill input")
    if DRILL_ANNOTATION not in drill_run:
        found.append(f"publish: the drill push lacks {DRILL_ANNOTATION}")
    if '"oci://$repo:verify-drill"' not in drill_run:
        found.append("publish: the drill push writes a tag other than verify-drill")
    return found + _refuses_problems(publish)


@pytest.fixture(scope="module")
def workflow() -> dict:
    return _load(WORKFLOW)


def test_the_drill_path_holds_every_property(workflow):
    assert drill_problems(workflow) == []


def _run_edit(step_id, old, new):
    def mutate(wf):
        step = _step(wf["jobs"]["publish"], step_id)
        step["run"] = step["run"].replace(old, new)

    return mutate


def _drop_refuses(wf):
    steps = wf["jobs"]["publish"]["steps"]
    steps[:] = [s for s in steps if s.get("id") != "drill-refuses"]


def _drifted_identity(wf):
    env = _step(wf["jobs"]["publish"], "drill-refuses")["env"]
    env["IDENTITY"] = env["IDENTITY"].replace("promote.yml", "ci.yml")


RED = {
    "a drill push without the annotation": (
        _run_edit("drill-push", DRILL_ANNOTATION, ""),
        "the drill push lacks",
    ),
    "a signed push carrying the annotation": (
        _run_edit("push", "--reproducible", f"{DRILL_ANNOTATION} --reproducible"),
        "the signed push carries the drill annotation",
    ),
    "a drill path without the must-fail verify": (
        _drop_refuses,
        "has no step `drill-refuses`",
    ),
    "a verify step that passes on a refusal": (
        _run_edit("drill-refuses", MUST_FAIL, "if ! cosign verify"),
        "drill-refuses does not fail",
    ),
    "a verify step that accepts any cosign failure": (
        _run_edit("drill-refuses", REFUSAL, "."),
        "accepts any cosign failure",
    ),
    "a verify step that skips the digest check": (
        _run_edit("drill-refuses", DIGEST_FORM, "true"),
        "does not check the digest's form",
    ),
    "a verify step under a drifted identity": (
        _drifted_identity,
        "an identity forge.py lacks",
    ),
}


@pytest.mark.parametrize("case", RED, ids=list(RED))
def test_each_broken_drill_property_is_named(workflow, case):
    mutate, expected = RED[case]
    broken = copy.deepcopy(workflow)
    mutate(broken)
    found = drill_problems(broken)
    assert any(expected in line for line in found), found
