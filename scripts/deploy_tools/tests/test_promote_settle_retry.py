"""The `gh` calls and the GHCR login in `promote.yml` retry a transient failure.

Each case runs a step's own `run:` under the fakes of `test_promote_settle.py`. A failure on
every try still fails the step, which that module's failed-write tests hold.

Run: uv run pytest scripts/deploy_tools/tests/test_promote_settle_retry.py
"""

import pytest
from test_promote_settle import (
    DIGEST,
    DISPATCH,
    HOLD,
    NEWER,
    OLDER,
    RECORD,
    RESET_PROD,
    T,
    _dispatch,
    _run,
    _settle,
    _step,
    jobs as jobs,
)


@pytest.mark.parametrize("job", ["tag", "reset-move"])
def test_a_prod_digest_write_recovers_on_its_third_try(tmp_path, jobs, job):
    script = _step(jobs[job], "Record prod's digest")["run"]
    result, calls = _run(
        tmp_path,
        script,
        FAKE_GH_FAIL="PROD_DIGEST",
        FAKE_GH_FAIL_TIMES="2",
        DIGEST=DIGEST,
    )
    assert result.returncode == 0, result.stderr
    assert len(calls) == 3
    assert "failed on try 2 of 3" in result.stderr


def test_settles_prod_digest_write_recovers_on_its_third_try(tmp_path, jobs):
    result, calls = _settle(
        tmp_path, jobs, FAKE_GH_FAIL="PROD_DIGEST", FAKE_GH_FAIL_TIMES="2"
    )
    assert result.returncode == 0, result.stderr
    assert calls == [HOLD, RESET_PROD, RECORD, RECORD, RECORD]


def test_the_dispatch_recovers_on_its_third_try(tmp_path, jobs):
    result, calls = _dispatch(
        tmp_path,
        jobs,
        [NEWER, T, OLDER],
        FAKE_GH_FAIL="workflow run",
        FAKE_GH_FAIL_TIMES="2",
    )
    assert result.returncode == 0, result.stderr
    assert calls == [DISPATCH] * 3


def test_the_run_list_read_recovers_without_keeping_the_failed_try_s_body(
    tmp_path, jobs
):
    """The red half is a capture that holds the error body, which fails the 40-hex check."""
    result, calls = _dispatch(
        tmp_path, jobs, [NEWER, T, OLDER], FAKE_GH_FAIL="api", FAKE_GH_FAIL_TIMES="1"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls == [DISPATCH]


def test_the_ghcr_login_recovers_and_sends_the_token_on_every_try(tmp_path, jobs):
    """The red half is a retry of `printf` alone, which logs in once."""
    script = _step(jobs["promote"], "Log in to GHCR read-only")["run"]
    result, _ = _run(
        tmp_path, script, FAKE_DOCKER_FAIL="login", FAKE_DOCKER_FAIL_TIMES="2"
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "calls").read_text().count("docker login ghcr.io") == 3
    assert (tmp_path / "calls.stdin").read_text() == "job-token" * 3


def test_a_ghcr_login_that_fails_every_try_fails_the_step(tmp_path, jobs):
    script = _step(jobs["promote"], "Log in to GHCR read-only")["run"]
    result, _ = _run(tmp_path, script, FAKE_DOCKER_FAIL="login")
    assert result.returncode != 0
    assert (tmp_path / "calls").read_text().count("docker login ghcr.io") == 3
