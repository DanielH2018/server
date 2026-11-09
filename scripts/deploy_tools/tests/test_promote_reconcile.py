"""The `verdict-status` and `reconcile` jobs' shell, run as written under fakes (#7865).

A run that ended without its `settle` (#7863) leaves `prod` apart from `last-good`.
`verdict-status` posts `verify`'s verdict on T as the commit status `homelab/promote-verdict`,
and a later run's `reconcile` reads it: a good status for `prod`'s digest moves `last-good`,
anything else writes the unverified hold. Both jobs check out nothing, so each test runs a
step's own `run:` under bash, with the `flux` fake and GHCR `curl` fake of
`test_promote_settle.py` and a `gh` fake that runs each `--jq` filter over FAKE_STATUSES.

Run: uv run pytest scripts/deploy_tools/tests/test_promote_reconcile.py
"""

import json
import shutil

import pytest
from lib import yaml_fast
from lib.proc_testing import fake_bin
from lib.proc_testing import run as proc_run
from test_promote_settle import FAKE_CURL, REPO_URL, WORKFLOW, _fake, _step

COMMIT = "c" * 40
DIGEST = "sha256:" + "1" * 64
LATER = "sha256:" + "f" * 64
CONTEXT = "homelab/promote-verdict"
MOVE = f"flux tag artifact {REPO_URL}@{DIGEST} --tag last-good"
HOLD = f"gh variable set DEPLOY_HOLD --body {COMMIT} unverified -R DanielH2018/server"

# Logs `gh <argv>`, fails when the argv contains FAKE_GH_FAIL, prints FAKE_HOLD for `variable
# get`, and answers `api ... --jq <filter>` with FAKE_STATUSES run through that filter.
FAKE_GH = r"""#!/usr/bin/env bash
echo "gh $*" >> "$CALLS"
if [ -n "${FAKE_GH_FAIL:-}" ] && [[ "$*" == *"$FAKE_GH_FAIL"* ]]; then
  echo "fake gh: forced failure" >&2
  exit 1
fi
if [ "$1 $2" = "variable get" ]; then printf '%s\n' "${FAKE_HOLD-none}"; exit 0; fi
if [ "$1" = api ]; then
  filter=
  while [ $# -gt 0 ]; do
    [ "$1" = --jq ] && filter=$2
    shift
  done
  [ -z "$filter" ] || printf '%s' "${FAKE_STATUSES:-[]}" | jq -r "$filter"
fi
"""


@pytest.fixture(scope="module")
def jobs() -> dict:
    return yaml_fast.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def _run(tmp_path, script: str, **env: str):
    """Run `script` under bash with the fakes, returning the result and the writes in order."""
    assert shutil.which("bash") and shutil.which("jq")
    calls = tmp_path / "calls"
    calls.write_text("")
    stub = fake_bin(
        tmp_path / "bin",
        flux=_fake("flux"),
        gh=FAKE_GH,
        docker=_fake("docker"),
        curl=FAKE_CURL,
        sleep="exit 0\n",
    )
    base = {
        "CALLS": str(calls),
        "GITHUB_REPOSITORY": "DanielH2018/server",
        "GH_ACTOR": "actor",
        "GITHUB_TOKEN": "job-token",
        "GH_TOKEN": "promote-token",
        "RUNNER_TEMP": str(tmp_path),
        "PATH": "/usr/bin:/bin",
        "RETRY_FUNCTIONS": yaml_fast.safe_load(WORKFLOW.read_text(encoding="utf-8"))[
            "env"
        ]["RETRY_FUNCTIONS"],
    }
    result = proc_run(["bash", "-c", script], env={**base, **env}, stub_bin=stub)
    lines = calls.read_text().splitlines()
    return result, [
        line
        for line in lines
        if line.startswith(("flux tag", "gh variable set", "gh api"))
    ]


def _statuses(*entries: tuple[str, str]) -> str:
    """A statuses list, newest first, of `(state, description)` verdicts among flux's own."""
    body = [{"context": "flux/homelab", "state": "success", "description": "applied"}]
    body += [
        {"context": CONTEXT, "state": state, "description": description}
        for state, description in entries
    ]
    return json.dumps(body)


def _reconcile(tmp_path, jobs, **env: str):
    script = _step(jobs["reconcile"], "Apply the lost run's verdict")["run"]
    defaults = {"COMMIT": COMMIT, "DIGEST": DIGEST, "FAKE_PROD_DIGEST": DIGEST}
    result, calls = _run(tmp_path, script, **{**defaults, **env})
    return result, [c for c in calls if not c.startswith("gh api")]


# -- reconcile -------------------------------------------------------------------------------


def test_a_good_status_for_prods_digest_moves_last_good_is_clean(tmp_path, jobs):
    """The #7863 tags: `prod` names the lost run's digest, which `last-good` does not."""
    result, calls = _reconcile(
        tmp_path, jobs, FAKE_STATUSES=_statuses(("success", f"good {DIGEST}"))
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls == [MOVE]


@pytest.mark.parametrize(
    "statuses",
    [
        _statuses(("failure", f"failed {DIGEST}")),
        _statuses(("failure", f"none {DIGEST}")),
        _statuses(),
        _statuses(("success", f"good {LATER}")),
        _statuses(("failure", f"failed {DIGEST}"), ("success", f"good {DIGEST}")),
    ],
    ids=["failed", "no-verdict", "missing", "another-digest", "newest-is-failure"],
)
def test_anything_but_a_good_status_for_prods_digest_writes_the_hold_is_flagged(
    tmp_path, jobs, statuses
):
    result, calls = _reconcile(tmp_path, jobs, FAKE_STATUSES=statuses)
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls == [HOLD], "last-good stays where it is"


def test_a_standing_hold_is_left_in_place(tmp_path, jobs):
    result, calls = _reconcile(tmp_path, jobs, FAKE_HOLD=f"{'b' * 40} failed=bento-pdf")
    assert (result.returncode, calls) == (0, [])
    assert "already reads" in result.stdout


@pytest.mark.parametrize("verdict", ["good", "failed"])
def test_a_prod_a_later_run_moved_writes_nothing(tmp_path, jobs, verdict):
    state = "success" if verdict == "good" else "failure"
    result, calls = _reconcile(
        tmp_path,
        jobs,
        FAKE_PROD_DIGEST=LATER,
        FAKE_STATUSES=_statuses((state, f"{verdict} {DIGEST}")),
    )
    assert (result.returncode, calls) == (0, [])
    assert "a later run moved it" in result.stdout


def test_an_unread_status_writes_nothing_and_fails(tmp_path, jobs):
    result, calls = _reconcile(tmp_path, jobs, FAKE_GH_FAIL="statuses")
    assert result.returncode != 0
    assert calls == []
    assert "could not be read" in result.stdout


def test_an_unread_prod_writes_nothing_and_fails(tmp_path, jobs):
    result, calls = _reconcile(tmp_path, jobs, FAKE_PROD_STATUS="503")
    assert result.returncode != 0
    assert calls == []


# -- verdict-status --------------------------------------------------------------------------


def _post(tmp_path, jobs, verdict: str):
    script = _step(jobs["verdict-status"], "Post the verdict status")["run"]
    env = {"TARGET": COMMIT, "DIGEST": DIGEST, "VERDICT": verdict, "RUN_URL": "u"}
    return _run(tmp_path, script, **env)


@pytest.mark.parametrize(
    ("verdict", "state", "described"),
    [
        ("good", "success", "good"),
        ("failed", "failure", "failed"),
        ("", "failure", "none"),
    ],
    ids=["good", "failed", "no-verdict"],
)
def test_each_verdict_posts_its_status_on_t_with_the_digest(
    tmp_path, jobs, verdict, state, described
):
    result, calls = _post(tmp_path, jobs, verdict)
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls == [
        f"gh api -X POST repos/DanielH2018/server/statuses/{COMMIT} -f state={state} "
        f"-f context={CONTEXT} -f description={described} {DIGEST} -f target_url=u"
    ]


def test_a_superseded_verdict_posts_nothing(tmp_path, jobs):
    result, calls = _post(tmp_path, jobs, "superseded")
    assert (result.returncode, calls) == (0, [])
