"""`settle` and `tag` write only while `prod` names the run's own target (slice D7.1).

`settle` applies a verdict on T only while `prod` still names T's digest, and reads `prod`
again before its reset. A superseding T resets to the live `last-good` when the superseded
run's `settle` moved it after T's `tag` read it (#6268). `tag` reads DEPLOY_HOLD again just before it moves `prod`. The cases
run each step's own `run:` under the fakes of `test_promote_settle.py`, as clean/flagged
pairs. docs/target-architecture/deploy/d7-superseding-promotions.md has the design.

Run: uv run pytest scripts/deploy_tools/tests/test_promote_settle_owner.py
"""

from test_promote_settle import (
    HOLD,
    RECORD,
    REPO_URL,
    RESET_PROD,
    T,
    _move,
    _outputs,
    _settle,
    jobs as jobs,
)

LATER = "sha256:" + "f" * 64
LAST_GOOD = f"flux tag artifact {REPO_URL}:sha-{T} --tag last-good"


def test_a_good_verdict_while_prod_names_t_moves_last_good_is_clean(tmp_path, jobs):
    result, calls = _settle(tmp_path, jobs, VERDICT="good", FAILED_NAMES="")
    assert result.returncode == 0, result.stderr
    assert calls == [LAST_GOOD]
    assert "owned=true" in _outputs(tmp_path)


def test_a_good_verdict_once_a_later_run_moved_prod_writes_nothing_is_flagged(
    tmp_path, jobs
):
    result, calls = _settle(
        tmp_path, jobs, VERDICT="good", FAILED_NAMES="", FAKE_PROD_DIGEST=LATER
    )
    assert result.returncode == 0, result.stderr
    assert calls == []
    assert "owned=false" in _outputs(tmp_path)
    assert "a later run moved it" in result.stdout


def test_a_failure_once_a_later_run_moved_prod_writes_no_hold_is_flagged(
    tmp_path, jobs
):
    result, calls = _settle(tmp_path, jobs, FAKE_PROD_DIGEST=LATER)
    assert (result.returncode, calls) == (0, [])


def test_a_stateful_failure_once_superseded_writes_no_hold_is_flagged(tmp_path, jobs):
    result, calls = _settle(
        tmp_path, jobs, STATEFUL="sonarr", FAILED_NAMES="sonarr", FAKE_PROD_DIGEST=LATER
    )
    assert (result.returncode, calls) == (0, [])


def test_a_move_between_the_hold_and_the_reset_skips_the_reset_is_flagged(
    tmp_path, jobs
):
    result, calls = _settle(tmp_path, jobs, FAKE_PROD_DIGEST_LATER=LATER)
    assert result.returncode == 0, result.stderr
    assert calls == [HOLD]
    assert RESET_PROD not in calls


def test_an_unreadable_prod_applies_the_verdict_as_before(tmp_path, jobs):
    result, calls = _settle(
        tmp_path, jobs, VERDICT="good", FAILED_NAMES="", FAKE_PROD_STATUS="503"
    )
    assert result.returncode == 0, result.stderr
    assert calls == [LAST_GOOD]
    assert "could not be read" in result.stdout


def test_the_dispatch_runs_only_for_the_run_that_owns_prod(jobs):
    step = next(
        s
        for s in jobs["settle"]["steps"]
        if s.get("name") == "Dispatch a run for a newer green commit"
    )
    assert step.get("if") == "steps.apply.outputs.owned == 'true'"


def test_a_move_with_no_hold_moves_prod_is_clean(tmp_path, jobs):
    result, calls, _ = _move(tmp_path, jobs)
    assert result.returncode == 0, result.stderr
    assert calls == [f"flux tag artifact {REPO_URL}:sha-{T} --tag prod"]


def test_a_hold_written_after_decide_moves_nothing_is_flagged(tmp_path, jobs):
    result, calls, output = _move(tmp_path, jobs, FAKE_HOLD=f"{T} failed=bento-pdf")
    assert result.returncode == 0, result.stderr
    assert (calls, output) == ([], "")
    assert "not the 'none' decide read" in result.stdout


def test_an_unreadable_hold_moves_nothing_and_fails(tmp_path, jobs):
    result, calls, output = _move(tmp_path, jobs, FAKE_GH_FAIL="variable get")
    assert result.returncode != 0
    assert (calls, output) == ([], "")


def test_a_superseded_verdict_writes_nothing_even_when_prod_cannot_be_read(
    tmp_path, jobs
):
    """`verify` read the move already; an unreadable `prod` here must not apply a verdict."""
    result, calls = _settle(
        tmp_path, jobs, VERDICT="superseded", FAILED_NAMES="", FAKE_PROD_STATUS="503"
    )
    assert (result.returncode, calls) == (0, [])
    assert "owned=false" in _outputs(tmp_path)


def test_verify_reads_prod_against_ts_digest(jobs):
    verify = jobs["verify"]
    step = next(s for s in verify["steps"] if s.get("id") == "verify")
    assert step["env"]["DIGEST"] == "${{ needs.promote.outputs.digest }}"
    assert '--digest "$DIGEST"' in step["run"]


def test_a_move_that_supersedes_tags_t_superseding_before_prod_is_flagged(
    tmp_path, jobs
):
    result, calls, _ = _move(tmp_path, jobs, SUPERSEDES="b" * 40)
    assert result.returncode == 0, result.stderr
    assert calls == [
        f"flux tag artifact {REPO_URL}:sha-{T} --tag superseding",
        f"flux tag artifact {REPO_URL}:sha-{T} --tag prod",
    ]


def test_a_failed_superseding_tag_moves_nothing(tmp_path, jobs):
    result, calls, output = _move(
        tmp_path, jobs, SUPERSEDES="b" * 40, FAKE_FLUX_FAIL="superseding"
    )
    assert result.returncode != 0
    assert calls == [f"flux tag artifact {REPO_URL}:sha-{T} --tag superseding"]
    assert output == ""


REVERT_HOLD = f"{T} failed=bento-pdf"


def test_a_revert_rule_move_under_the_hold_decide_read_moves_prod_is_clean(
    tmp_path, jobs
):
    """The revert rule promotes while a `failed=` hold stands, so that hold is not a refusal."""
    result, calls, _ = _move(
        tmp_path, jobs, FAKE_HOLD=REVERT_HOLD, HOLD_READ=REVERT_HOLD
    )
    assert result.returncode == 0, result.stderr
    assert calls == [f"flux tag artifact {REPO_URL}:sha-{T} --tag prod"]


def test_a_hold_that_changed_since_decide_read_it_moves_nothing_is_flagged(
    tmp_path, jobs
):
    result, calls, output = _move(
        tmp_path, jobs, FAKE_HOLD=f"{T} unverified", HOLD_READ=REVERT_HOLD
    )
    assert (result.returncode, calls, output) == (0, [], "")


def test_a_superseding_move_after_last_good_moved_moves_nothing_is_flagged(
    tmp_path, jobs
):
    """The superseded run settled first, so T's base and reset digest are stale."""
    result, calls, output = _move(
        tmp_path, jobs, SUPERSEDES="b" * 40, FAKE_LAST_GOOD_DIGEST="sha256:" + "9" * 64
    )
    assert (result.returncode, calls, output) == (0, [], "")
    assert "settled first" in result.stdout


def test_a_hold_after_a_supersession_names_the_superseded_commit(tmp_path, jobs):
    """The hold names T alone, so the log names the run whose change T also carried (#6199)."""
    result, calls = _settle(tmp_path, jobs, SUPERSEDES="b" * 40)
    assert result.returncode == 0, result.stderr
    assert calls[0] == HOLD
    assert f"superseded the verification of {'b' * 40}" in result.stdout


P_GOOD = "sha256:" + "9" * 64


def test_a_superseding_failure_after_the_superseded_run_settled_good_resets_to_it(
    tmp_path, jobs
):
    """The superseded run moved `last-good` after this run's `tag` read it (#6268)."""
    result, calls = _settle(
        tmp_path, jobs, SUPERSEDES="b" * 40, FAKE_LAST_GOOD_DIGEST=P_GOOD
    )
    assert result.returncode == 0, result.stderr
    assert calls == [
        HOLD,
        f"flux tag artifact {REPO_URL}@{P_GOOD} --tag prod",
        f"gh variable set PROD_DIGEST --body {P_GOOD} -R DanielH2018/server",
    ]


def test_a_failure_that_supersedes_nothing_keeps_the_reset_digest_decide_read(
    tmp_path, jobs
):
    result, calls = _settle(tmp_path, jobs, FAKE_LAST_GOOD_DIGEST=P_GOOD)
    assert result.returncode == 0, result.stderr
    assert calls == [HOLD, RESET_PROD, RECORD]


def test_a_superseding_failure_with_an_unreadable_last_good_resets_to_decides_read(
    tmp_path, jobs
):
    result, calls = _settle(
        tmp_path,
        jobs,
        SUPERSEDES="b" * 40,
        FAKE_LAST_GOOD_DIGEST=P_GOOD,
        FAKE_LAST_GOOD_STATUS="503",
    )
    assert result.returncode == 0, result.stderr
    assert calls == [HOLD, RESET_PROD, RECORD]
    assert "last-good could not be read" in result.stdout
