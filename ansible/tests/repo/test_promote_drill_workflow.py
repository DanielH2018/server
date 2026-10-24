"""A `verify_drill` dispatch of `promote.yml` pushes a bundle no signed push can carry.

WHY THIS IS A TEST. The drill is meant to make the `verify-drill` OCIRepository read
Ready=False. On 2026-10-13 it verified instead (#4820): `--reproducible` gave the drill the
same digest as the `sha-<X>` bundle `publish` had already signed, and cosign v3 stores a
signature as a referrer of the digest, so every tag on that digest verifies. The drill push
therefore carries an annotation the signed push never does, and the same dispatch proves its
own red half: `cosign verify` under promote's identity must refuse the drill digest, and the
registry must answer that the digest has no referrer and no legacy `.sig` tag. cosign's refusal
alone is not enough, because cosign v3.1.3 reads a registry error on its bundle probe as "no
bundle" (#4829). The last test runs the step's own script against a fake cosign and curl, since
`workflow_run` never runs a branch's copy of the file.

These checks sit beside `test_promote_workflow.py` rather than in it because that module was
near the test-module line cap. `drill_problems` names every broken property. The real
file must produce none, and each red fixture breaks one.

Run: uv run pytest ansible/tests/repo/test_promote_drill_workflow.py
"""

import copy
import os
import shutil

import forge
import pytest
from lib.proc_testing import fake_bin
from lib.proc_testing import run as proc_run
from test_promote_workflow import WORKFLOW, _load, _steps

DRILL_ANNOTATION = "--annotations=io.homelab.verify-drill=true"
DRILL_IF = "${{ inputs.verify_drill }}"
SIGNED_IF = "${{ !inputs.verify_drill }}"
DRILL_DIGEST = "${{ steps.drill-push.outputs.digest }}"
# `if cosign verify ...; then exit 1` fails the run when verification succeeds.
MUST_FAIL = "if cosign verify"
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


# A fake curl answers by the path it is asked for. FAKE_<KEY>_STATUS and FAKE_<KEY>_BODY set
# the answer, and a key left unset answers 404 with no body. The pull-token call runs with -f
# and no -w, so it gets the body on stdout.
FAKE_CURL = r"""#!/usr/bin/env bash
cat > /dev/null
out= fmt= url=
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out=$2; shift 2 ;;
    -w) fmt=$2; shift 2 ;;
    -H|-K|--retry) shift 2 ;;
    -*) shift ;;
    *) url=$1; shift ;;
  esac
done
case "$url" in
  *"/token?"*) key=TOKEN ;;
  *"/referrers/"*) key=REFERRERS ;;
  *"/manifests/sha256:"*) key=SUBJECT ;;
  *.sig) key=SIG ;;
  *"/manifests/sha256-"*) key=FALLBACK ;;
  *) echo "fake curl: unexpected $url" >&2; exit 2 ;;
esac
status_var=FAKE_${key}_STATUS body_var=FAKE_${key}_BODY
status=${!status_var:-404} body=${!body_var:-}
if [ -n "$out" ]; then printf '%s' "$body" > "$out"; else printf '%s' "$body"; fi
if [ -n "$fmt" ]; then printf '%s' "$status"; fi
"""

# cosign v3.1.3's message on a signed digest whose referrers call failed: #4829's case.
FAKE_COSIGN = """#!/usr/bin/env bash
echo 'Error: no signatures found' >&2
exit "${FAKE_COSIGN_RC:-1}"
"""

# The index prod's referrers tag held on 2026-10-13.
SIGNED_INDEX = (
    '{"schemaVersion":2,"mediaType":"application/vnd.oci.image.index.v1+json","manifests":'
    '[{"mediaType":"application/vnd.oci.image.manifest.v1+json","size":879,"digest":'
    '"sha256:dc50f90e827a5763d7b1e2bb79fbc4c1b718d2c67324999e868e0b7648d8e85e",'
    '"artifactType":"application/vnd.dev.sigstore.bundle.v0.3+json"}]}'
)

# Each case: the fake answers, and the text the failed run must print, or None for a pass.
REGISTRY_CASES = {
    "no referrer and no .sig tag, as the drill digest read on 2026-10-13": ({}, None),
    "a referrers API that answers with an empty index": (
        {"FAKE_REFERRERS_STATUS": "200", "FAKE_REFERRERS_BODY": '{"manifests":[]}'},
        None,
    ),
    "a signed digest whose referrers call got a 429": (
        {"FAKE_REFERRERS_STATUS": "429"},
        "the referrers API answered HTTP 429",
    ),
    "a referrers tag that answered 503": (
        {"FAKE_FALLBACK_STATUS": "503"},
        "answered HTTP 503",
    ),
    "a referrers tag holding a Sigstore bundle": (
        {"FAKE_FALLBACK_STATUS": "200", "FAKE_FALLBACK_BODY": SIGNED_INDEX},
        "sigstore.bundle",
    ),
    "a referrers API listing a Sigstore bundle": (
        {"FAKE_REFERRERS_STATUS": "200", "FAKE_REFERRERS_BODY": SIGNED_INDEX},
        "sigstore.bundle",
    ),
    "a legacy .sig tag": ({"FAKE_SIG_STATUS": "200"}, "legacy signature tag"),
    "a legacy .sig tag that answered 401": (
        {"FAKE_SIG_STATUS": "401"},
        "answered HTTP 401",
    ),
    "a token that cannot see the drill digest": (
        {"FAKE_SUBJECT_STATUS": "404"},
        "own manifest answered HTTP 404",
    ),
    "a digest cosign verifies": (
        {"FAKE_COSIGN_RC": "0"},
        "cosign verified the drill digest",
    ),
}


@pytest.mark.parametrize("case", REGISTRY_CASES, ids=list(REGISTRY_CASES))
def test_the_refusal_step_passes_only_on_a_registry_that_holds_no_signature(
    workflow, tmp_path, case
):
    assert shutil.which("jq"), "the step parses the registry's answers with jq"
    answers, expected = REGISTRY_CASES[case]
    env = {
        **os.environ,
        "RUNNER_TEMP": str(tmp_path),
        "DIGEST": "sha256:" + "5b" * 32,
        "IDENTITY": forge.SIGNER_IDENTITY,
        "ISSUER": forge.SIGNER_ISSUER,
        "GH_ACTOR": "actor",
        "GITHUB_TOKEN": "fake",
        "FAKE_TOKEN_STATUS": "200",
        "FAKE_TOKEN_BODY": '{"token":"fake"}',
        "FAKE_SUBJECT_STATUS": "200",
        **answers,
    }
    run = _step(workflow["jobs"]["publish"], "drill-refuses")["run"]
    result = proc_run(
        ["bash", "-c", run],
        env=env,
        stub_bin=fake_bin(tmp_path / "bin", curl=FAKE_CURL, cosign=FAKE_COSIGN),
    )
    output = result.stdout + result.stderr
    if expected is None:
        assert result.returncode == 0, output
    else:
        assert result.returncode != 0, output
        assert expected in output, output
