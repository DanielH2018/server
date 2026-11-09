"""Pre-flight (can the tick cross what is incoming?) and the master CI wait.

Run: uv run pytest scripts/deploy_tools/tests/test_land_ci.py
"""

import pytest

from _land_fakes import PRIMARY, Fakes
from deploy_tools.land_lib import ci
from deploy_tools.land_lib.outcome import Outcome
from deploy_tools.land_lib.tools import CiVerdict


def test_preflight_passes_when_nothing_is_incoming(landing):
    ln, calls = landing()
    ci.preflight(ln)
    assert calls[-1] == ("deploy_tags", ("blockers", "origin/master"), {"cwd": PRIMARY})


def test_blocked_when_an_incoming_change_needs_a_hand(landing):
    ln, _ = landing(Fakes(blockers=[3]))
    with pytest.raises(Outcome) as exc:
        ci.preflight(ln)
    assert (exc.value.rc, exc.value.verdict) == (1, "blocked")
    assert "see above" not in exc.value.detail


def test_a_blocked_verdict_names_the_path_and_the_command(landing):
    """cc-wait relays only the VERDICT line, so the blockers document's facts go in it (#7895)."""
    doc = '{"needing_hand": ["ansible/bootstrap.yml"], "run": "uv run ansible-playbook x"}'
    ln, _ = landing(Fakes(blockers=[(3, doc + "\n")]))
    with pytest.raises(Outcome) as exc:
        ci.preflight(ln)
    assert "ansible/bootstrap.yml" in exc.value.detail
    assert "uv run ansible-playbook x" in exc.value.detail


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
        ci.wait_master_ci(ln, "abc")
    assert (exc.value.rc, exc.value.verdict) == (code, verdict)


def test_step_3_names_the_sha_and_omits_the_on_clause_for_a_timeout(landing):
    ln, _ = landing(Fakes(await_ci=[(1, "x")]))
    with pytest.raises(Outcome) as red:
        ci.wait_master_ci(ln, "abc123")
    assert (
        red.value.error
        == "master CI is RED on abc123 — land.sh deployed nothing itself"
    )

    ln, _ = landing(Fakes(await_ci=[(75, "x")]), ci_timeout=900)
    with pytest.raises(Outcome) as timeout:
        ci.wait_master_ci(ln, "abc123")
    assert (
        timeout.value.error
        == "no CI verdict inside 900s — land.sh deployed nothing itself"
    )


def test_an_unrecognized_await_ci_exit_is_named(landing):
    ln, _ = landing(Fakes(await_ci=[(3, "x")]))
    with pytest.raises(Outcome) as exc:
        ci.wait_master_ci(ln, "abc123")
    assert (
        exc.value.error == "await_ci failed (exit 3) — land.sh deployed nothing itself"
    )


def test_green_returns_and_prints_the_line(landing, capsys):
    ln, _ = landing(Fakes(await_ci=[(0, "0123456: CI green")]))
    ci.wait_master_ci(ln, "abc")
    assert "0123456: CI green" in capsys.readouterr().out


# await_ci's own line for a disarmed gate, as land_lib.tools.await_ci_verdict builds it.
_DISARMED_LINE = "await_ci: no required CI contexts -- refusing to report a verdict"


def _disarmed_output(landing, capsys) -> str:
    """Everything land.sh prints when await_ci exits CI_DISARMED, stdout and stderr."""
    from lib.exit_codes import CI_DISARMED

    ln, _ = landing(Fakes(await_ci=[(CI_DISARMED, _DISARMED_LINE)]))
    with pytest.raises(Outcome) as exc:
        ci.wait_master_ci(ln, "abc123")
    exc.value.emit()
    out = capsys.readouterr()
    return out.out + out.err


def test_a_disarmed_gate_is_named_as_disarmed(landing, capsys):
    """Exit 2 is CI_DISARMED, not a failed CI run: the output says so (#4464)."""
    out = _disarmed_output(landing, capsys)
    assert "disarmed" in out.lower(), out
    assert "(exit 2)" not in out, out


def test_a_disarmed_gate_names_where_to_re_arm_it(landing, capsys):
    """The next step: land.sh's arm state is await_ci.REQUIRED_CONTEXTS (#4464)."""
    from deploy_tools import await_ci

    assert await_ci.REQUIRED_CONTEXTS, "the gate is armed at HEAD"
    out = _disarmed_output(landing, capsys)
    assert "REQUIRED_CONTEXTS" in out or "await_ci.py" in out, out


def _step_3_over_master(
    landing, tmp_path, monkeypatch, green: list[bool], red: frozenset[int] = frozenset()
) -> tuple[list[str], Outcome | None]:
    """Run step 3's wait on the first of a scratch master's commits; None when it returned.

    `green[i]` says whether commit i went green. A commit that did not has no check run and
    no check suite at all, which is what GitHub left for a dropped push event (#4621), unless
    its index is in `red`, which gives it a failed required run instead. The
    REAL await_ci.wait runs over a fake clock and a fake GitHub, and the origin is a real
    repository, so the wait reaches a descendant through whichever git query a fix chooses.
    """
    import await_ci
    from lib.git_testing import commit, git, init_repo, scrub_process_git_env

    scrub_process_git_env(monkeypatch)
    origin = init_repo(tmp_path / "origin.git", bare=True)
    clone = init_repo(tmp_path / "clone", initial_commit="base")
    git(clone, "remote", "add", "origin", str(origin))
    shas = [commit(clone, f"merge {i}", **{f"f{i}": "1"}) for i in range(len(green))]
    git(clone, "push", "-q", "origin", "master")
    # _fetch_tip and _is_ancestor run git with no cwd, as from land.sh's primary checkout.
    monkeypatch.chdir(clone)

    passed = [
        {"name": name, "status": "completed", "conclusion": "success"}
        for name in await_ci.REQUIRED_CONTEXTS
    ]
    failed = [{**run, "conclusion": "failure"} for run in passed]
    runs = {
        sha: (passed if ok else failed if i in red else [])
        for i, (sha, ok) in enumerate(zip(shas, green, strict=True))
    }
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    def real_wait(sha, timeout_s):
        return CiVerdict(
            *await_ci.wait(
                sha,
                timeout_s,
                20,
                sleep=sleep,
                clock=lambda: now[0],
                fetch=lambda s: runs.get(s, []),
                fetch_suites=lambda s: [],
            )
        )

    ln, _ = landing(ci_timeout=900)
    ln.merge_sha = shas[0]
    ln.tools.await_ci = real_wait
    try:
        ci.wait_master_ci(ln, ln.merge_sha)
    except Outcome as exc:
        return shas, exc
    return shas, None


def test_step_3_does_not_time_out_on_a_merge_commit_github_never_ran_ci_for(
    landing, tmp_path, monkeypatch
):
    """#4621: no push run ever exists for the merge commit, and the next master commit is green.

    Step 3 waited on the merge commit alone and ended `ci-timeout`, the verdict whose resume
    is a re-run, which entered the same wait on the same SHA. Either it now accepts the green
    descendant and returns, or it ends on a verdict of its own that names the descendant and
    the promote dispatch.
    """
    (_, tip), ended = _step_3_over_master(landing, tmp_path, monkeypatch, [False, True])
    if ended is None:
        return  # Step 3 accepted the green descendant, and the landing goes on.
    detail = ended.error or ""
    assert ended.verdict != "ci-timeout", detail
    assert ended.rc != 75, detail
    assert tip[:8] in detail, detail
    assert "promote.yml" in detail, detail


def test_step_3_promote_dispatch_names_the_tip_not_an_older_green_commit(
    landing, tmp_path, monkeypatch
):
    """#4621's second recurrence: the tip has no run either, and only a middle commit is green.

    A verdict of its own has to name the tip, whose promotion carries the merge commit.
    Accepting the green descendant and returning is also a fix.
    """
    (_, middle, tip), ended = _step_3_over_master(
        landing, tmp_path, monkeypatch, [False, True, False]
    )
    if ended is None:
        return  # Step 3 accepted the green descendant, and the landing goes on.
    detail = ended.error or ""
    assert ended.verdict != "ci-timeout", detail
    assert tip[:8] in detail, detail
    assert f"-f sha={middle}" not in detail, detail
    assert "promote.yml" in detail, detail


def test_step_3_accepts_the_green_middle_commit_when_the_tip_has_no_run_either(
    landing, tmp_path, monkeypatch, capsys
):
    """#4621's second recurrence through the accept path: the verdict is the middle commit's.

    The run-less tip stays unaccepted, and the step-3 line names the commit it trusted.
    """
    (_, middle, tip), ended = _step_3_over_master(
        landing, tmp_path, monkeypatch, [False, True, False]
    )
    assert ended is None
    out = capsys.readouterr().out
    assert f"{middle[:8]}: CI green" in out, out
    assert f"{tip[:8]}: CI green" not in out, out


@pytest.mark.parametrize(
    "green", [[False], [False, False]], ids=["merge-is-tip", "all-run-less"]
)
def test_step_3_names_the_tip_when_no_commit_from_the_merge_on_has_a_run(
    landing, tmp_path, monkeypatch, green
):
    """#4641: the run-less merge commit is the tip, or every commit after it is run-less too.

    No re-run converges until somebody merges something, so step 3 must not end
    `ci-timeout` (whose resume is a re-run). Its own verdict names the tip, whichever route
    out it then offers.
    """
    shas, ended = _step_3_over_master(landing, tmp_path, monkeypatch, green)
    tip = shas[-1]
    assert ended is not None, "no commit is green, so step 3 cannot accept a verdict"
    detail = ended.error or ""
    assert ended.verdict != "ci-timeout", detail
    assert ended.rc != 75, detail
    assert tip[:8] in detail, detail


def test_step_3_does_not_end_red_for_good_once_a_later_descendant_is_green(
    landing, tmp_path, monkeypatch
):
    """#4649: the merge commit's push was dropped, D1 went red on its own bug, D2 is green.

    The nearest descendant's red never changes, so every re-run ended `ci-red` while the
    tick fast-forwarded past the merge commit to D2. Either step 3 now converges, or the
    verdict it ends on names the tip and a promote dispatch out of this state.
    """
    (_, _, tip), ended = _step_3_over_master(
        landing, tmp_path, monkeypatch, [False, False, True], red=frozenset({1})
    )
    if ended is None:
        return  # Step 3 accepted the green tip, and the landing goes on.
    detail = ended.error or ""
    assert ended.verdict != "ci-timeout", detail
    assert tip[:8] in detail, detail
    if ended.verdict == "ci-red":
        assert "promote.yml" in detail, detail


def test_an_unproven_merge_commit_ends_ci_unproven_and_names_the_way_out(landing):
    """#4641/#4649: exit 1, not the 75 resume point, carrying await_ci's line and the tags."""
    from lib.exit_codes import CI_UNPROVEN

    line = "abc12345: no verdict can arrive — run `gh workflow run ci.yml --ref master`"
    ln, _ = landing(Fakes(await_ci=[(CI_UNPROVEN, line)]))
    ln.resolved_tags = ["grafana", "loki"]
    with pytest.raises(Outcome) as exc:
        ci.wait_master_ci(ln, "abc123")
    assert (exc.value.rc, exc.value.verdict) == (1, "ci-unproven")
    assert exc.value.error == (
        f"no CI verdict can arrive: {line} (this PR's tags: grafana,loki) "
        "— land.sh deployed nothing itself"
    )
