"""The `tag` and `settle` jobs' shell, run as written under fake `flux`, `gh` and `docker`.

Both jobs check out no repository code, so their rules live in the step text of
`.github/workflows/promote.yml`: the `last-good` seed, the hold written before the reset, the
PROD_DIGEST write after every move of `prod`, and the dispatch of a run for a newer green
commit, which `test_promote_dispatch.py` holds. Each test runs a step's own `run:` under bash,
with the workflow's env functions loaded and fakes that append their argv to one log, then asserts the calls and their order.

Run: uv run pytest scripts/deploy_tools/tests/promote/test_promote_settle.py
"""

import re
import shutil

import pytest
from lib import yaml_fast
from lib.proc_testing import fake_bin
from lib.proc_testing import run as proc_run
from lib.repo_paths import REPO

WORKFLOW = REPO / ".github" / "workflows" / "promote.yml"
REPO_URL = "oci://ghcr.io/danielh2018/homelab-manifests"
T = "a" * 40
DIGEST = "sha256:" + "d" * 64
RESET = "sha256:" + "e" * 64
# The move step's outputs: `moved`, and the UTC time stamped just before the move.
MOVED = r"moved=true\nmoved_at=\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\n"

_FAKE = """#!/usr/bin/env bash
[ "{tool}" = docker ] && cat >> "$CALLS.stdin"
echo "{tool} $*" >> "$CALLS"
if [ -n "${{FAKE_{upper}_FAIL:-}}" ] && [[ "$*" == *"$FAKE_{upper}_FAIL"* ]]; then
  if [ -z "${{FAKE_{upper}_FAIL_TIMES:-}}" ] \\
    || [ "$(grep -cF -- "$FAKE_{upper}_FAIL" "$CALLS")" -le "$FAKE_{upper}_FAIL_TIMES" ]; then
    echo "fake {tool}: error body"
    echo "fake {tool}: forced failure" >&2
    exit 1
  fi
fi
if [ "{tool}" = gh ] && [ "$1" = api ]; then printf '%s\\n' "${{FAKE_GREEN:-}}"; fi
if [ "{tool}" = gh ] && [ "$1 $2" = "variable get" ]; then printf '%s\\n' "${{FAKE_HOLD-none}}"; fi
"""


def _fake(tool: str) -> str:
    return _FAKE.format(tool=tool, upper=tool.upper())


FAKE_CURL = r"""#!/usr/bin/env bash
cat > /dev/null
out= url=
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out=$2; shift 2 ;;
    -w|-H|-K|--retry) shift 2 ;;
    -*) shift ;;
    *) url=$1; shift ;;
  esac
done
case "$url" in
  *"/token?"*) printf '{"token":"t"}' ;;
  *"/manifests/prod")
    status=${FAKE_PROD_STATUS:-200} digest=$FAKE_PROD_DIGEST
    [ -e "$RUNNER_TEMP/prod-read" ] && digest=${FAKE_PROD_DIGEST_LATER:-$digest}
    touch "$RUNNER_TEMP/prod-read"
    printf 'HTTP/2 %s\r\nDocker-Content-Digest: %s\r\n\r\n' "$status" "$digest" > "$out"
    printf '%s' "$status" ;;
  *"/manifests/last-good")
    status=${FAKE_LAST_GOOD_STATUS:-200}
    printf 'HTTP/2 %s\r\nDocker-Content-Digest: %s\r\n\r\n' "$status" "${FAKE_LAST_GOOD_DIGEST-$RESET_DIGEST}" > "$out"
    printf '%s' "$status" ;;
  *) echo "fake curl: unexpected $url" >&2; exit 2 ;;
esac
"""


@pytest.fixture(scope="module")
def jobs() -> dict:
    return yaml_fast.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def _functions() -> dict[str, str]:
    """The workflow env's shell functions, which each step loads with `eval`."""
    env = yaml_fast.safe_load(WORKFLOW.read_text(encoding="utf-8")).get("env", {})
    keys = ("RETRY_FUNCTIONS", "REGISTRY_FUNCTIONS", "DISPATCH_FUNCTIONS")
    return {key: env.get(key, "") for key in keys}


def _step(job: dict, name: str) -> dict:
    return next(s for s in job["steps"] if s.get("name") == name)


def _run(tmp_path, script: str, **env: str):
    """Run `script` under bash with the fakes, returning the result and the calls in order."""
    assert shutil.which("bash")
    calls = tmp_path / "calls"
    calls.write_text("")
    stub = fake_bin(
        tmp_path / "bin",
        flux=_fake("flux"),
        gh=_fake("gh"),
        docker=_fake("docker"),
        curl=FAKE_CURL,
        sleep="exit 0\n",
    )
    base = {
        "CALLS": str(calls),
        "GITHUB_REPOSITORY": "DanielH2018/server",
        "GITHUB_OUTPUT": str(tmp_path / "github_output"),
        "GH_ACTOR": "actor",
        "GITHUB_TOKEN": "job-token",
        "GH_TOKEN": "promote-token",
        "RUNNER_TEMP": str(tmp_path),
        "PATH": "/usr/bin:/bin",
        **_functions(),
    }
    result = proc_run(["bash", "-c", script], env={**base, **env}, stub_bin=stub)
    lines = [line for line in calls.read_text().splitlines() if line]
    return result, [l for l in lines if not l.startswith(("docker", "gh variable get"))]


def _settle(tmp_path, jobs, **env: str):
    script = _step(jobs["settle"], "Apply the verdict")["run"]
    defaults = {
        "TARGET": T,
        "DIGEST": DIGEST,
        "FAKE_PROD_DIGEST": DIGEST,
        "RESET_DIGEST": RESET,
        "CLEARS_HOLD": "false",
        "HOLD_FORM": "reset",
        "VERDICT": "failed",
        "FAILED_NAMES": "bento-pdf",
    }
    return _run(tmp_path, script, **{**defaults, **env})


HOLD = f"gh variable set DEPLOY_HOLD --body {T} failed=bento-pdf -R DanielH2018/server"
RESET_PROD = f"flux tag artifact {REPO_URL}@{RESET} --tag prod"
RECORD = f"gh variable set PROD_DIGEST --body {RESET} -R DanielH2018/server"


# -- settle: a good verdict ------------------------------------------------------------------


def test_a_good_verdict_moves_last_good_to_ts_digest_and_writes_no_hold(tmp_path, jobs):
    result, calls = _settle(tmp_path, jobs, VERDICT="good", FAILED_NAMES="")
    assert result.returncode == 0, result.stderr
    assert calls == [f"flux tag artifact {REPO_URL}@{DIGEST} --tag last-good"]


def test_a_good_verdict_from_the_revert_rule_also_clears_the_hold(tmp_path, jobs):
    result, calls = _settle(tmp_path, jobs, VERDICT="good", CLEARS_HOLD="true")
    assert result.returncode == 0, result.stderr
    assert calls == [
        f"flux tag artifact {REPO_URL}@{DIGEST} --tag last-good",
        "gh variable set DEPLOY_HOLD --body none -R DanielH2018/server",
    ]


def test_a_malformed_digest_moves_nothing_even_when_prod_is_unreadable(tmp_path, jobs):
    result, calls = _settle(
        tmp_path, jobs, VERDICT="good", DIGEST="sha-" + T, FAKE_PROD_STATUS="503"
    )
    assert result.returncode != 0
    assert calls == []
    assert "is not sha256:<64 hex>" in result.stdout


# -- settle: anything else holds, then resets -----------------------------------------------


def test_a_failure_writes_the_hold_before_the_reset_then_records_prod_digest(
    tmp_path, jobs
):
    result, calls = _settle(tmp_path, jobs)
    assert result.returncode == 0, result.stderr
    assert calls == [HOLD, RESET_PROD, RECORD]


def test_a_hold_write_that_fails_still_resets_and_exits_non_zero_naming_both(
    tmp_path, jobs
):
    result, calls = _settle(tmp_path, jobs, FAKE_GH_FAIL="DEPLOY_HOLD")
    assert result.returncode != 0
    assert calls == [HOLD, HOLD, HOLD, RESET_PROD, RECORD]
    assert "writing DEPLOY_HOLD" in result.stdout
    assert f"prod reset to {RESET}" in result.stdout


def test_a_reset_that_fails_after_the_hold_exits_non_zero_with_the_hold_in_place(
    tmp_path, jobs
):
    result, calls = _settle(tmp_path, jobs, FAKE_FLUX_FAIL="--tag prod")
    assert result.returncode != 0
    assert calls == [HOLD, RESET_PROD], "no PROD_DIGEST for a move that did not happen"
    assert "resetting prod" in result.stdout


def test_a_prod_digest_write_that_fails_exits_non_zero_after_the_reset(tmp_path, jobs):
    result, calls = _settle(tmp_path, jobs, FAKE_GH_FAIL="PROD_DIGEST")
    assert result.returncode != 0
    assert calls == [HOLD, RESET_PROD, RECORD, RECORD, RECORD]
    assert "recording PROD_DIGEST failed" in result.stdout


@pytest.mark.parametrize(
    "verdict, names",
    [("", ""), ("failed", ""), ("failed", "bento-pdf; rm -rf /")],
    ids=["no-verdict", "no-names", "malformed-names"],
)
def test_a_verify_with_no_usable_verdict_holds_as_unverified(
    tmp_path, jobs, verdict, names
):
    result, calls = _settle(tmp_path, jobs, VERDICT=verdict, FAILED_NAMES=names)
    unverified = (
        f"gh variable set DEPLOY_HOLD --body {T} unverified -R DanielH2018/server"
    )
    assert calls == [unverified, RESET_PROD, RECORD]
    assert result.returncode == 0, result.stderr


def test_a_failure_with_no_last_good_digest_holds_and_does_not_reset(tmp_path, jobs):
    result, calls = _settle(tmp_path, jobs, RESET_DIGEST="")
    assert result.returncode != 0
    assert calls == [HOLD]
    assert "no last-good digest" in result.stdout


@pytest.mark.parametrize(
    ("verdict", "names"),
    [("failed", "longhorn"), ("", "")],
    ids=["failed", "unverified"],
)
def test_a_forward_only_failure_holds_as_t_alone_and_leaves_prod(
    tmp_path, jobs, verdict, names
):
    result, calls = _settle(
        tmp_path, jobs, HOLD_FORM="no-reset", VERDICT=verdict, FAILED_NAMES=names
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls == [
        f"gh variable set DEPLOY_HOLD --body {T} -R DanielH2018/server",
        f"flux tag artifact {REPO_URL}@{DIGEST} --tag held",
    ]


def test_a_failed_hold_write_tags_nothing_and_exits_non_zero(tmp_path, jobs):
    result, calls = _settle(
        tmp_path, jobs, HOLD_FORM="no-reset", FAKE_GH_FAIL="DEPLOY_HOLD"
    )
    assert result.returncode != 0
    assert (
        calls == [f"gh variable set DEPLOY_HOLD --body {T} -R DanielH2018/server"] * 3
    )


def test_a_failed_held_tag_exits_non_zero_with_the_hold_in_place(tmp_path, jobs):
    result, calls = _settle(tmp_path, jobs, HOLD_FORM="no-reset", FAKE_FLUX_FAIL="held")
    assert result.returncode != 0
    assert calls[0] == f"gh variable set DEPLOY_HOLD --body {T} -R DanielH2018/server"
    assert "tagging sha-" in result.stdout


HELD = f"flux tag artifact {REPO_URL}@{DIGEST} --tag held"


def _stateful_hold(value: str) -> str:
    return f"gh variable set DEPLOY_HOLD --body {T} {value} -R DanielH2018/server"


def _outputs(tmp_path) -> str:
    output = tmp_path / "github_output"
    return output.read_text() if output.exists() else ""


def test_a_failure_that_changes_a_snapshot_role_holds_stateful_and_does_not_reset(
    tmp_path, jobs
):
    """The no-reset rule and the hold's form.

    The red half is C2's `failed=` form followed by a reset, which the same fixture without
    HOLD_FORM=stateful produces (`test_a_failure_writes_the_hold_before_the_reset_then_records_prod_digest`).
    """
    result, calls = _settle(tmp_path, jobs, HOLD_FORM="stateful", FAILED_NAMES="sonarr")
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls == [_stateful_hold("stateful=sonarr"), HELD]
    assert f"stateful_hold={T} stateful=sonarr\n" in _outputs(tmp_path)


def test_a_snapshot_role_change_with_a_failing_non_snapshot_role_holds_without_reset(
    tmp_path, jobs
):
    """D5 Open question 1, case (a).

    The trigger is a changed snapshot role, not a failed one, so traefik's failure leaves
    `prod` on T, and the hold names traefik.
    """
    result, calls = _settle(
        tmp_path, jobs, HOLD_FORM="stateful", FAILED_NAMES="traefik"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls == [_stateful_hold("stateful=traefik"), HELD]


@pytest.mark.parametrize(
    "verdict, names",
    [("", ""), ("failed", ""), ("failed", "sonarr;reboot")],
    ids=["no-verdict", "no-names", "malformed-names"],
)
def test_a_stateful_failure_with_no_usable_names_holds_as_stateful_unverified(
    tmp_path, jobs, verdict, names
):
    result, calls = _settle(
        tmp_path, jobs, HOLD_FORM="stateful", VERDICT=verdict, FAILED_NAMES=names
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls == [_stateful_hold("stateful=unverified"), HELD]


@pytest.mark.parametrize("form", ["", "stateful-ish"], ids=["empty", "unknown"])
def test_a_hold_form_decide_never_writes_holds_nothing_and_resets_nothing(
    tmp_path, jobs, form
):
    result, calls = _settle(tmp_path, jobs, HOLD_FORM=form)
    assert result.returncode != 0
    assert calls == []
    assert "is not stateful, no-reset or reset" in result.stdout


def test_a_failed_stateful_hold_write_tags_nothing_and_leaves_no_post(tmp_path, jobs):
    result, calls = _settle(
        tmp_path, jobs, HOLD_FORM="stateful", FAKE_GH_FAIL="DEPLOY_HOLD"
    )
    assert result.returncode != 0
    assert calls == [_stateful_hold("stateful=bento-pdf")] * 3
    assert "stateful_hold=" not in _outputs(tmp_path)


# -- tag: the seed and the move --------------------------------------------------------------


def _move(tmp_path, jobs, **env: str):
    assert shutil.which("jq"), "the step reads the pull token with jq"
    script = _step(jobs["tag"], "Move prod")["run"]
    defaults = {
        "TARGET": T,
        "DIGEST": DIGEST,
        "SEED": "false",
        "PRIOR_DIGEST": RESET,
        "RESET_DIGEST": RESET,
        "FAKE_PROD_DIGEST": RESET,
    }
    result, calls = _run(tmp_path, script, **{**defaults, **env})
    output = tmp_path / "github_output"
    return result, calls, output.read_text() if output.exists() else ""


def test_a_first_verified_run_seeds_last_good_before_prod_moves(tmp_path, jobs):
    result, calls, output = _move(tmp_path, jobs, SEED="true")
    assert result.returncode == 0, result.stderr
    assert calls == [
        f"flux tag artifact {REPO_URL}@{RESET} --tag last-good",
        f"flux tag artifact {REPO_URL}@{DIGEST} --tag prod",
    ]
    assert re.fullmatch(MOVED, output), output


def test_a_run_with_last_good_present_moves_prod_alone(tmp_path, jobs):
    result, calls, output = _move(tmp_path, jobs)
    assert result.returncode == 0, result.stderr
    assert calls == [f"flux tag artifact {REPO_URL}@{DIGEST} --tag prod"]
    assert re.fullmatch(MOVED, output), output


def test_a_failed_seed_moves_nothing_and_reports_no_move(tmp_path, jobs):
    result, calls, output = _move(
        tmp_path, jobs, SEED="true", FAKE_FLUX_FAIL="last-good"
    )
    assert result.returncode != 0
    assert calls == [f"flux tag artifact {REPO_URL}@{RESET} --tag last-good"]
    assert output == "", "verify and settle run only once prod has moved"


def test_a_prod_another_run_moved_first_is_left_alone_and_nothing_is_verified(
    tmp_path, jobs
):
    """Two runs decided on the same `prod`; the first `tag` moved it, so the second stops."""
    result, calls, output = _move(tmp_path, jobs, FAKE_PROD_DIGEST=DIGEST)
    assert result.returncode == 0, result.stderr
    assert calls == []
    assert output == "", "verify and settle skip a run that moved nothing"
    assert "another run moved it first" in result.stdout


def test_a_seed_run_whose_prod_moved_seeds_nothing(tmp_path, jobs):
    result, calls, output = _move(tmp_path, jobs, SEED="true", FAKE_PROD_DIGEST=DIGEST)
    assert (result.returncode, calls, output) == (0, [], "")


def test_a_first_run_moves_prod_when_prod_is_still_absent(tmp_path, jobs):
    result, calls, output = _move(
        tmp_path, jobs, PRIOR_DIGEST="", FAKE_PROD_STATUS="404", FAKE_PROD_DIGEST=""
    )
    assert result.returncode == 0, result.stderr
    assert calls == [f"flux tag artifact {REPO_URL}@{DIGEST} --tag prod"]
    assert re.fullmatch(MOVED, output), output


def test_a_prod_read_that_fails_moves_nothing(tmp_path, jobs):
    result, calls, output = _move(tmp_path, jobs, FAKE_PROD_STATUS="503")
    assert result.returncode != 0
    assert (calls, output) == ([], "")
    assert "prod's digest could not be read" in result.stdout
    assert "HEAD prod answered HTTP 503" in result.stderr


def test_the_promotion_records_prod_digest_after_the_move_and_fails_on_a_failed_write(
    tmp_path, jobs
):
    names = [s.get("name") for s in jobs["tag"]["steps"]]
    assert names.index("Record prod's digest") > names.index("Move prod")
    record = _step(jobs["tag"], "Record prod's digest")
    assert "steps.move.outputs.moved == 'true'" in record["if"], (
        "only a move is recorded"
    )
    script = _step(jobs["tag"], "Record prod's digest")["run"]
    result, calls = _run(tmp_path, script, DIGEST=DIGEST, FAKE_GH_FAIL="PROD_DIGEST")
    assert (
        calls
        == [f"gh variable set PROD_DIGEST --body {DIGEST} -R DanielH2018/server"] * 3
    )
    assert result.returncode != 0
