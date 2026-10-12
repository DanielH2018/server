"""Pre-flight (can the tick cross what is incoming?) and the master CI wait.

Run: uv run pytest scripts/deploy_tools/tests/test_land_ci.py
"""

import pytest

from _land_fakes import PRIMARY, Fakes
from deploy_tools.land_lib import ci
from deploy_tools.land_lib.outcome import Outcome
from deploy_tools.land_lib.tools import CiVerdict


def test_preflight_passes_when_nothing_is_incoming(landing, capsys):
    ln, calls = landing()
    ci.preflight(ln)
    assert "nothing in the way" in capsys.readouterr().out
    assert calls[-1] == ("deploy_tags", ("blockers", "origin/master"), {"cwd": PRIMARY})


def test_blocked_when_an_incoming_change_needs_a_hand(landing):
    ln, _ = landing(Fakes(blockers=[3]))
    with pytest.raises(Outcome) as exc:
        ci.preflight(ln)
    assert (exc.value.rc, exc.value.verdict) == (1, "blocked")


def test_a_broken_preflight_is_a_plain_failure(landing):
    """Not `blocked`: nothing in the range was judged, so it takes the exit-1 catch-all."""
    ln, _ = landing(Fakes(blockers=[1]))
    with pytest.raises(Outcome) as exc:
        ci.preflight(ln)
    assert exc.value.rc == 1 and exc.value.verdict == "refused"


@pytest.mark.parametrize(
    "rc, verdict, code", [(1, "ci-red", 1), (75, "ci-timeout", 75)]
)
def test_red_and_no_verdict_each_end_the_landing_with_a_name(
    landing, rc, verdict, code
):
    ln, _ = landing(Fakes(await_ci=[(rc, "x")]))
    with pytest.raises(Outcome) as exc:
        ci.wait_master_ci(ln, "abc", "abc")
    assert (exc.value.rc, exc.value.verdict) == (code, verdict)


def test_step_3_names_the_sha_and_omits_the_on_clause_for_a_timeout(landing):
    """The unlabelled caller is step 3, waiting on the merge commit itself."""
    ln, _ = landing(Fakes(await_ci=[(1, "x")]))
    with pytest.raises(Outcome) as red:
        ci.wait_master_ci(ln, "abc123")
    assert red.value.error == "master CI is RED on abc123 — nothing deployed"

    ln, _ = landing(Fakes(await_ci=[(75, "x")]), ci_timeout=900)
    with pytest.raises(Outcome) as timeout:
        ci.wait_master_ci(ln, "abc123")
    assert timeout.value.error == "no CI verdict inside 900s — nothing deployed"


def test_a_labelled_wait_names_what_it_waited_on(landing):
    """The stale-retry caller waits on a tip that is not this PR's merge commit."""
    ln, _ = landing(Fakes(await_ci=[(75, "x")]), ci_timeout=900)
    with pytest.raises(Outcome) as exc:
        ci.wait_master_ci(ln, "def456", "the tip def456")
    assert (
        exc.value.error
        == "no CI verdict on the tip def456 inside 900s — nothing deployed"
    )


def test_a_labelled_red_names_that_the_tick_cannot_cross_it(landing):
    """A labelled tip wait says "the tick cannot cross it" for a red CI on the tip; the
    unlabelled step-3 wait does not."""
    ln, _ = landing(Fakes(await_ci=[(1, "x")]))
    with pytest.raises(Outcome) as exc:
        ci.wait_master_ci(ln, "def456", "the tip def456")
    assert exc.value.error == (
        "master CI is RED on the tip def456 — the tick cannot cross it; nothing deployed"
    )


def test_an_unrecognized_await_ci_exit_names_what_it_waited_on(landing):
    """The catch-all `await_ci failed` branch must keep `where`, so a labelled (tip) wait and
    an unlabelled (step 3) wait read differently on any exit other than 1 or 75."""
    ln, _ = landing(Fakes(await_ci=[(3, "x")]))
    with pytest.raises(Outcome) as unlabelled:
        ci.wait_master_ci(ln, "abc123")
    assert unlabelled.value.error == "await_ci failed (exit 3) — nothing deployed"

    ln, _ = landing(Fakes(await_ci=[(3, "x")]))
    with pytest.raises(Outcome) as labelled:
        ci.wait_master_ci(ln, "def456", "the tip def456")
    assert (
        labelled.value.error
        == "await_ci failed on the tip def456 (exit 3) — nothing deployed"
    )


def test_green_returns_and_prints_the_line(landing, capsys):
    ln, _ = landing(Fakes(await_ci=[(0, "0123456: CI green")]))
    ci.wait_master_ci(ln, "abc", "abc")
    assert "0123456: CI green" in capsys.readouterr().out


# await_ci's own line for a disarmed gate, as land_lib.tools.await_ci_verdict builds it.
_DISARMED_LINE = "await_ci: no required CI contexts -- refusing to report a verdict"


def _disarmed_output(landing, capsys, label: str | None = None) -> str:
    """Everything land.sh prints when await_ci exits CI_DISARMED, stdout and stderr."""
    from lib.exit_codes import CI_DISARMED

    ln, _ = landing(Fakes(await_ci=[(CI_DISARMED, _DISARMED_LINE)]))
    with pytest.raises(Outcome) as exc:
        ci.wait_master_ci(ln, "abc123", label)
    exc.value.emit()
    out = capsys.readouterr()
    return out.out + out.err


@pytest.mark.parametrize("label", [None, "the tip def456"])
def test_a_disarmed_gate_is_named_as_disarmed(landing, capsys, label):
    """Exit 2 is CI_DISARMED, not a failed CI run: the output says so (#4464)."""
    out = _disarmed_output(landing, capsys, label)
    assert "disarmed" in out.lower(), out
    assert "(exit 2)" not in out, out


def test_a_disarmed_gate_names_where_to_re_arm_it(landing, capsys):
    """The next step: land.sh's arm state is await_ci.REQUIRED_CONTEXTS (#4464)."""
    from deploy_tools import await_ci

    assert await_ci.REQUIRED_CONTEXTS, "the gate is armed at HEAD"
    out = _disarmed_output(landing, capsys)
    assert "REQUIRED_CONTEXTS" in out or "await_ci.py" in out, out


def test_step_3_does_not_time_out_on_a_merge_commit_github_never_ran_ci_for(
    landing, tmp_path, monkeypatch
):
    """#4621: no push run ever exists for the merge commit, and a later master commit is green.

    Step 3 waited on the merge commit alone and ended `ci-timeout`, the verdict whose resume
    is a re-run, which entered the same wait on the same SHA. Either it now accepts the green
    descendant and returns, or it ends on a verdict of its own that names the descendant and
    the hand-deploy command. deploy.sh refuses a tree behind origin/master, so the command
    has to name the tip, not an older green commit.
    """
    import await_ci
    from lib.git_testing import commit, git, init_repo, scrub_process_git_env

    scrub_process_git_env(monkeypatch)
    origin = init_repo(tmp_path / "origin.git", bare=True)
    clone = init_repo(tmp_path / "clone", initial_commit="base")
    git(clone, "remote", "add", "origin", str(origin))
    merge = commit(clone, "the squash merge GitHub never ran CI for", a="1")
    tip = commit(clone, "a later merge that went green", b="1")
    git(clone, "push", "-q", "origin", "master")
    monkeypatch.chdir(clone)

    required = next(iter(await_ci.REQUIRED_CONTEXTS))
    green = [{"name": required, "status": "completed", "conclusion": "success"}]
    runs_by_sha: dict[str, list[dict]] = {merge: [], tip: green}
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    def real_wait(sha, timeout_s):
        # The real wait over a fake clock and a fake GitHub; only the transport is stubbed.
        return CiVerdict(
            *await_ci.wait(
                sha,
                timeout_s,
                20,
                sleep=sleep,
                clock=lambda: now[0],
                fetch=lambda s: runs_by_sha.get(s, []),
                fetch_suites=lambda s: [],
            )
        )

    ln, _ = landing(ci_timeout=900)
    ln.merge_sha = merge
    ln.tools.await_ci = real_wait
    ended: Outcome | None = None
    try:
        ci.wait_master_ci(ln, ln.merge_sha)
    except Outcome as exc:
        ended = exc
    if ended is None:
        return  # green: step 3 accepted the descendant and the landing goes on
    detail = ended.error or ""
    assert ended.verdict != "ci-timeout", detail
    assert ended.rc != 75, detail
    assert tip[:8] in detail, detail
    assert "deploy.sh" in detail, detail
