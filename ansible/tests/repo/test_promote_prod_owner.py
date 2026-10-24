"""The `promote` workflow writes only while `prod` names the run's own target (slice D7.1).

`settle` applies a verdict on T only while `prod` still names T's digest, and reads `prod`
again before its reset. `tag` reads DEPLOY_HOLD again just before it moves `prod`. Both run
here as bash, against fake `curl`, `flux`, `gh` and `docker` binaries, so each case checks the
writes the step actually makes rather than the text it holds.
docs/target-architecture/deploy/d7-superseding-promotions.md has the design.

Run: uv run pytest ansible/tests/repo/test_promote_prod_owner.py
"""

import os
import shutil
from pathlib import Path

import pytest
from lib.proc_testing import fake_bin
from lib.proc_testing import run as proc_run
from test_promote_workflow import WORKFLOW, _load, _steps

TARGET = "a" * 40
DIGEST = "sha256:" + "1a" * 32
LATER = "sha256:" + "2b" * 32
LAST_GOOD = "sha256:" + "3c" * 32

# Answers the pull-token call with a token, and each HEAD of `prod` with the next digest in
# FAKE_PROD, one per call, recorded in a counter file. A digest of `500` answers HTTP 500.
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
  *"/token?"*) printf '{"token":"fake"}'; exit 0 ;;
  *"/manifests/prod") ;;
  *) echo "fake curl: unexpected $url" >&2; exit 2 ;;
esac
n=$(cat "$RUNNER_TEMP/heads" 2>/dev/null || echo 0)
echo $((n + 1)) > "$RUNNER_TEMP/heads"
read -r -a digests <<< "$FAKE_PROD"
digest=${digests[$n]:-${digests[-1]}}
if [ "$digest" = 500 ]; then printf '500'; exit 0; fi
printf 'HTTP/2 200\r\ndocker-content-digest: %s\r\n' "$digest" > "$out"
printf '200'
"""
FAKE_FLUX = 'echo "flux $*" >> "$RUNNER_TEMP/writes"\n'
FAKE_GH = r"""#!/usr/bin/env bash
if [ "$1 $2" = "variable get" ]; then
  [ -n "${FAKE_HOLD:-}" ] || exit 1
  printf '%s\n' "$FAKE_HOLD"; exit 0
fi
echo "gh $*" >> "$RUNNER_TEMP/writes"
"""
FAKE_DOCKER = "cat > /dev/null\n"


@pytest.fixture(scope="module")
def workflow() -> dict:
    return _load(WORKFLOW)


def _run_step(workflow: dict, job: str, name: str, tmp_path: Path, **env: str):
    """Run one step's script; return its exit code, output, writes and `$GITHUB_OUTPUT`."""
    assert shutil.which("jq"), "the steps parse the registry's token answer with jq"
    step = next(s for s in _steps(workflow["jobs"][job]) if s.get("name") == name)
    outputs = tmp_path / "github_output"
    outputs.touch()
    result = proc_run(
        ["bash", "-c", step["run"]],
        env={
            **os.environ,
            "RUNNER_TEMP": str(tmp_path),
            "GITHUB_OUTPUT": str(outputs),
            "GITHUB_REPOSITORY": "owner/repo",
            "GH_ACTOR": "actor",
            "GITHUB_TOKEN": "fake",
            "TARGET": TARGET,
            "DIGEST": DIGEST,
            **env,
        },
        stub_bin=fake_bin(
            tmp_path / "bin",
            curl=FAKE_CURL,
            flux=FAKE_FLUX,
            gh=FAKE_GH,
            docker=FAKE_DOCKER,
        ),
    )
    writes = tmp_path / "writes"
    return (
        result.returncode,
        result.stdout + result.stderr,
        writes.read_text() if writes.exists() else "",
        outputs.read_text(),
    )


def _settle(workflow, tmp_path, prod: str, **env: str):
    return _run_step(
        workflow,
        "settle",
        "Apply the verdict",
        tmp_path,
        **{
            "FAKE_PROD": prod,
            "RESET_DIGEST": LAST_GOOD,
            "CLEARS_HOLD": "false",
            "NO_RESET": "",
            "STATEFUL": "",
            "FAILED_NAMES": "",
            **env,
        },
    )


def test_settle_moves_last_good_while_prod_names_t(workflow, tmp_path):
    code, out, writes, outputs = _settle(workflow, tmp_path, DIGEST, VERDICT="good")
    assert code == 0, out
    assert "--tag last-good" in writes, out
    assert "owned=true" in outputs


def test_settle_writes_nothing_once_a_later_run_moved_prod(workflow, tmp_path):
    code, out, writes, outputs = _settle(workflow, tmp_path, LATER, VERDICT="good")
    assert code == 0, out
    assert writes == "", writes
    assert "owned=false" in outputs
    assert "a later run moved it" in out


def test_settle_holds_and_resets_a_failed_t_it_owns(workflow, tmp_path):
    code, out, writes, _ = _settle(
        workflow, tmp_path, DIGEST, VERDICT="failed", FAILED_NAMES="bento-pdf"
    )
    assert code == 0, out
    assert f"DEPLOY_HOLD --body {TARGET} failed=bento-pdf" in writes, out
    assert f"@{LAST_GOOD} --tag prod" in writes, out


def test_settle_skips_the_reset_when_prod_moves_after_the_hold(workflow, tmp_path):
    code, out, writes, _ = _settle(
        workflow,
        tmp_path,
        f"{DIGEST} {LATER}",
        VERDICT="failed",
        FAILED_NAMES="bento-pdf",
    )
    assert code == 0, out
    assert "failed=bento-pdf" in writes, out
    assert "--tag prod" not in writes, writes


def test_settle_writes_no_stateful_hold_once_superseded(workflow, tmp_path):
    code, out, writes, _ = _settle(
        workflow,
        tmp_path,
        LATER,
        VERDICT="failed",
        FAILED_NAMES="sonarr",
        STATEFUL="sonarr",
    )
    assert code == 0, out
    assert writes == "", writes


def test_settle_applies_the_verdict_when_prod_cannot_be_read(workflow, tmp_path):
    code, out, writes, outputs = _settle(workflow, tmp_path, "500", VERDICT="good")
    assert code == 0, out
    assert "--tag last-good" in writes, out
    assert "could not be read" in out
    assert "owned=true" in outputs


def _move(workflow, tmp_path, **env: str):
    return _run_step(
        workflow,
        "tag",
        "Move prod",
        tmp_path,
        FAKE_PROD=LAST_GOOD,
        PRIOR_DIGEST=LAST_GOOD,
        SEED="false",
        **env,
    )


def test_tag_moves_prod_with_no_hold(workflow, tmp_path):
    code, out, writes, outputs = _move(workflow, tmp_path, FAKE_HOLD="none")
    assert code == 0, out
    assert f"sha-{TARGET} --tag prod" in writes, out
    assert "moved=true" in outputs


def test_tag_moves_nothing_under_a_hold_written_after_decide(workflow, tmp_path):
    code, out, writes, outputs = _move(
        workflow, tmp_path, FAKE_HOLD=f"{TARGET} failed=bento-pdf"
    )
    assert code == 0, out
    assert writes == "", writes
    assert "moved=true" not in outputs


def test_tag_moves_nothing_when_the_hold_cannot_be_read(workflow, tmp_path):
    code, out, writes, outputs = _move(workflow, tmp_path, FAKE_HOLD="")
    assert code != 0, out
    assert writes == "", writes
    assert "moved=true" not in outputs
