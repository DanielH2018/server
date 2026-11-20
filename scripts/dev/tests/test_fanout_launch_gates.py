"""The launch gates: what `launch` refuses before it spends an ssh connection.

Run: uv run pytest scripts/dev/tests/test_fanout_launch_gates.py
"""

import dataclasses
import json
import os
import socket
import threading
from concurrent.futures import ThreadPoolExecutor, wait

import pytest

from dev.fanout_lib import launch_lock
from dev.findings_lib.issue_model import Issue, IssueState
from dev.fanout_lib.manifest import Batch, Manifest, save
from dev.fanout_lib.place import main
from _fanout_fakes import HOST_KEY, fake_tools, ok

# Roomy on both planes and holding a registered key, so a refusal here comes from a gate
# rather than from placement or the signing gate.
HEADROOM = f"1\n12884901888\n1\n12884901888\n0\n{HOST_KEY}\n"
CLAIMED = [
    Issue(1, "one", IssueState.OPEN, body="body one", labels=frozenset({"claude"})),
    Issue(2, "two", IssueState.OPEN, body="body two", labels=frozenset({"claude"})),
]


def _launch(tools, tmp_path, *args):
    return main(
        [
            "launch",
            *args,
            "--manifest-root",
            str(tmp_path),
        ],
        tools,
    )


def test_live_batches_from_an_earlier_run_count_against_the_hosts_cap(tmp_path, capsys):
    """Headroom alone underprices agents launched minutes ago, so count them.

    On a host other than this one: a remote host's unit state costs an ssh connection the
    budget has no room for, so its batches stay counted until `clean` runs.
    """
    remote = _another_host()
    # Six, so the host is full whichever one `remote` is: both caps are 6.
    numbers = (91, 92, 93, 94, 95, 96)
    earlier = [Batch(f"{n}", remote, "/w", "b", "u", [n], "t") for n in numbers]
    save(Manifest("20260101T000010Z", "o", earlier), root=tmp_path)
    tools, run = fake_tools(answers={remote: ok(HEADROOM)}, issues=CLAIMED)
    assert _launch(tools, tmp_path, "--host", remote, "--batch", "1,2") == 3
    err = capsys.readouterr().err
    assert f"{remote} already holds 6 live batch(es)" in err
    assert "91 in run 20260101T000010Z" in err
    assert "run `clean <run-id>`" in err
    # The headroom read happened; no launch call did.
    assert len(run.calls) == 1


def test_a_cleaned_batch_does_not_count_against_the_cap(tmp_path):
    """The red half's twin: `clean` is what frees a slot, so a removed batch must not hold one."""
    earlier = [
        Batch(f"{n}", "daniel-box", "/w", "b", "u", [n], "t", removed_at="t")
        for n in (91, 92, 93)
    ]
    save(Manifest("20260101T000010Z", "o", earlier), root=tmp_path)
    tools, run = fake_tools(answers={"daniel-box": ok(HEADROOM)}, issues=CLAIMED)
    assert _launch(tools, tmp_path, "--host", "daniel-box", "--batch", "1,2") == 0
    assert len(run.host_calls) == 3  # headroom read, health read, one launch


def _another_host() -> str:
    return next(h for h in ("daniel-box", "daniel-server") if h != socket.gethostname())


def _unit_state(batch: str, running: bool) -> str:
    """One batch's block of `status_command`'s output: an active unit, or one that exited 1."""
    props = (
        "ActiveState=active\nResult=success\nExecMainStatus=0"
        if running
        else "ActiveState=failed\nResult=exit-code\nExecMainStatus=1"
    )
    return f"=== {batch}\n{props}\n--- stderr\n\n--- report\n\n--- verdict\n"


def _uncleaned(tmp_path, host, count=3):
    """`count` uncleaned batches on `host`, whose unit states the headroom read can carry."""
    numbers = range(91, 91 + count)
    earlier = [Batch(f"{n}", host, "/w", "b", "u", [n], "t") for n in numbers]
    save(Manifest("20260101T000010Z", "o", earlier), root=tmp_path)


def _headroom_with(*blocks):
    """A headroom reply that also carries unit blocks, as `read_host` asks for them."""
    return ok(HEADROOM + "--- unit states\n" + "".join(blocks))


def _launch_pinned(tmp_path, host, reply, batches=("1",)):
    """A server launch pinned to `host`, whose headroom read answers `reply`."""
    tools, run = fake_tools(answers={host: reply}, issues=CLAIMED)
    flags = [arg for spec in batches for arg in ("--batch", spec)]
    code = _launch(tools, tmp_path, "--host", host, *flags)
    return code, run


def _clear(tmp_path):
    for stale in tmp_path.glob("*.json"):
        stale.unlink()


def test_finished_batches_do_not_count_against_the_cap_on_either_host(tmp_path):
    """3 uncleaned batches, 2 finished, so 1 running and the launch places.

    Run on both fleet hosts by name rather than by the hostname of the machine running the
    test, which is not a fleet host on a CI runner.
    """
    for host in ("daniel-box", "daniel-server"):
        _uncleaned(tmp_path, host)
        reply = _headroom_with(
            _unit_state("91", running=False),
            _unit_state("92", running=True),
            _unit_state("93", running=False),
        )
        code, _ = _launch_pinned(tmp_path, host, reply)
        assert code == 0, host
        _clear(tmp_path)


def test_reading_the_unit_states_makes_no_extra_ssh_call(tmp_path):
    """The ssh budget is 5 per 30s: headroom, health, one launch is 3, with units or not."""
    remote = "daniel-server"
    _uncleaned(tmp_path, remote)
    reply = _headroom_with(*(_unit_state(str(n), running=False) for n in (91, 92, 93)))
    code, run = _launch_pinned(tmp_path, remote, reply)
    assert code == 0
    assert len(run.host_calls) == 3  # headroom read, health read, one launch
    headroom, *rest = run.host_calls
    assert "systemctl --user show" in headroom[1]
    assert not any("systemctl --user show" in command for _, command, _ in rest)


def _running(tmp_path, host, count):
    """`count` uncleaned batches on `host`, each reported running by the headroom read."""
    numbers = range(91, 91 + count)
    earlier = [Batch(f"{n}", host, "/w", "b", "u", [n], "t") for n in numbers]
    save(Manifest("20260101T000010Z", "o", earlier), root=tmp_path)
    return _headroom_with(*(_unit_state(str(n), running=True) for n in numbers))


def test_six_running_batches_refuse_a_seventh_on_daniel_box(tmp_path, capsys):
    reply = _running(tmp_path, "daniel-box", 6)
    code, run = _launch_pinned(tmp_path, "daniel-box", reply)
    assert code == 3
    err = capsys.readouterr().err
    assert "daniel-box already holds 6 live batch(es)" in err
    assert "over the 6 that" in err
    assert len(run.host_calls) == 1


def test_daniel_server_admits_a_sixth_running_batch(tmp_path):
    reply = _running(tmp_path, "daniel-server", 5)
    code, _ = _launch_pinned(tmp_path, "daniel-server", reply)
    assert code == 0


def test_six_running_batches_refuse_a_seventh_on_daniel_server(tmp_path, capsys):
    reply = _running(tmp_path, "daniel-server", 6)
    code, run = _launch_pinned(tmp_path, "daniel-server", reply)
    assert code == 3
    err = capsys.readouterr().err
    assert "daniel-server already holds 6 live batch(es)" in err
    assert "over the 6 that" in err
    assert len(run.host_calls) == 1


def test_a_unit_state_that_cannot_be_read_counts_as_running(tmp_path, capsys):
    """Fail closed: no marker, an empty section and a missing block each leave the batch counted.

    On daniel-box, whose cap of 6 is what the arithmetic below is written against.
    """
    remote = "daniel-box"
    replies = (
        ok(HEADROOM),
        _headroom_with(),
        # One finished batch, and no block at all for the other five.
        _headroom_with(_unit_state("91", running=False)),
    )
    for reply in replies:
        _uncleaned(tmp_path, remote, count=6)
        # Two new batches: the 5 or 6 live ones plus 2 exceed the cap of 6, and would not if
        # an unreadable state counted as stopped.
        code, _ = _launch_pinned(tmp_path, remote, reply, batches=("1", "2"))
        assert code == 3
        assert "live batch(es)" in capsys.readouterr().err
        _clear(tmp_path)


def test_a_dotfiles_launch_runs_here_and_reads_the_dotfiles_register(tmp_path):
    here = socket.gethostname()
    issue = Issue(
        763,
        "dotfiles finding",
        IssueState.OPEN,
        body="body",
        labels=frozenset({"claude"}),
    )
    tools, run = fake_tools(answers={here: ok(HEADROOM)}, issues=[issue])
    assert (
        _launch(tools, tmp_path, "--repo", "DanielH2018/dotfiles", "--batch", "763")
        == 0
    )
    assert run.issue_fetches == [(763, "DanielH2018/dotfiles")]
    assert {host for host, _, _ in run.calls} == {here, "findings"}
    (manifest,) = tmp_path.glob("*.json")
    (batch,) = json.loads(manifest.read_text())["batches"]
    assert batch["repo"] == "DanielH2018/dotfiles" and batch["host"] == here
    assert batch["unit"] == "fanout-dotfiles-763"


def test_a_dotfiles_launch_pinned_to_another_host_is_refused(tmp_path, capsys):
    issue = Issue(
        763,
        "dotfiles finding",
        IssueState.OPEN,
        body="body",
        labels=frozenset({"claude"}),
    )
    tools, run = fake_tools(issues=[issue])
    other = _another_host()
    argv = ["--repo", "DanielH2018/dotfiles", "--host", other, "--batch", "763"]
    assert _launch(tools, tmp_path, *argv) == 64
    assert not run.calls and not run.issue_fetches
    assert "runs on this host" in capsys.readouterr().err


def test_a_live_batch_in_another_repo_does_not_hold_the_same_issue_number(tmp_path):
    """The clean half of `test_a_batch_still_live_in_a_manifest_is_refused_...`."""
    live = Batch(
        "1-2", "daniel-server", "/w", "b", "u", [1, 2], "t", repo="DanielH2018/dotfiles"
    )
    save(Manifest("20260101T000010Z", "o", [live]), root=tmp_path)
    tools, _ = fake_tools(answers={"daniel-box": ok(HEADROOM)}, issues=CLAIMED)
    assert _launch(tools, tmp_path, "--host", "daniel-box", "--batch", "1,2") == 0


def test_two_interleaved_launches_cannot_both_pass_the_live_batch_cap(tmp_path):
    """The second launch starts while the first is between its gates and its manifest save.

    daniel-box holds 5 of its 6, so each launch alone fits and the two together do not.
    """
    reply = _running(tmp_path, "daniel-box", 5)
    placing, release = threading.Event(), threading.Event()
    tools, run = fake_tools(answers={"daniel-box": reply}, issues=CLAIMED)

    def stopped_at_the_launch_call(host, command, timeout, stdin=None):
        if "worktree add" in command:
            placing.set()
            assert release.wait(30)
        return run(host, command, timeout, stdin)

    first_tools = dataclasses.replace(tools, run=stopped_at_the_launch_call)
    pinned = ("--host", "daniel-box", "--batch")
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(_launch, first_tools, tmp_path, *pinned, "1")
        assert placing.wait(30)
        second_tools, _ = fake_tools(answers={"daniel-box": reply}, issues=CLAIMED)
        second = pool.submit(_launch, second_tools, tmp_path, *pinned, "2")
        # Long enough for an unserialised second launch to run every gate and return.
        wait([second], timeout=1)
        release.set()
    assert (first.result(), second.result()) == (0, 3)


def test_a_launch_that_cannot_take_the_lock_exits_8_naming_the_holder(tmp_path, capsys):
    tools, run = fake_tools(answers={"daniel-box": ok(HEADROOM)}, issues=CLAIMED)
    with launch_lock.held(tmp_path, lambda: "20260101T000010Z"):
        code = _launch(tools, tmp_path, "--batch", "1", "--lock-wait", "0.05")
    assert code == 8
    holder = f"launch lock held by pid {os.getpid()} (run 20260101T000010Z)"
    assert holder in capsys.readouterr().err
    assert not run.calls and not run.issue_fetches


ALERTS = "scripts/diagnostics/probe_lib/alerts.py"
SOPS = "secrets/secrets.yml"
HELD_BRANCH = "worktree-fanout-91"


def _held_elsewhere(tmp_path, cited):
    """Batch 91, uncleaned, in a run another coordinator launched on daniel-box."""
    held = Batch(
        "91", "daniel-box", "/w", HELD_BRANCH, "fanout-91", [91], "t", cited_paths=cited
    )
    save(Manifest("20260101T000010Z", "worktree-project-a", [held]), root=tmp_path)


def _citing(path):
    """Issue 1 for the new batch and issue 91 for the held one, both citing `path`."""
    return [
        Issue(
            n,
            "t",
            IssueState.OPEN,
            body=f"`{path}` drops a row",
            labels=frozenset({"claude"}),
        )
        for n in (1, 91)
    ]


@pytest.mark.parametrize("recorded", [(ALERTS,), None], ids=["recorded", "legacy"])
def test_launch_refuses_a_file_a_batch_with_an_open_pr_in_another_run_cites(
    tmp_path, capsys, recorded
):
    """A batch saved before `cited_paths` existed is read from its issue bodies instead."""
    _held_elsewhere(tmp_path, recorded)
    tools, run = fake_tools(
        answers={"daniel-box": ok(HEADROOM)},
        issues=_citing(ALERTS),
        open_prs={HELD_BRANCH},
    )
    assert _launch(tools, tmp_path, "--host", "daniel-box", "--batch", "1") == 1
    assert not run.calls
    err = capsys.readouterr().err
    assert (
        f"{ALERTS}: batch 1 cites it; batch 91 holds it with an open PR "
        "(run 20260101T000010Z, orchestrator branch `worktree-project-a`)"
    ) in err


def test_a_standing_batch_with_a_stopped_unit_and_no_open_pr_holds_no_file(tmp_path):
    _held_elsewhere(tmp_path, (ALERTS,))
    reply = _headroom_with(_unit_state("91", running=False))
    tools, _ = fake_tools(answers={"daniel-box": reply}, issues=_citing(ALERTS))
    assert _launch(tools, tmp_path, "--host", "daniel-box", "--batch", "1") == 0


def test_a_batch_whose_agent_still_runs_holds_its_files_before_it_opens_a_pr(
    tmp_path, capsys
):
    _held_elsewhere(tmp_path, (ALERTS,))
    reply = _headroom_with(_unit_state("91", running=True))
    tools, _ = fake_tools(answers={"daniel-box": reply}, issues=_citing(ALERTS))
    assert _launch(tools, tmp_path, "--host", "daniel-box", "--batch", "1") == 1
    assert f"{ALERTS}: batch 1 cites it; batch 91 holds it" in capsys.readouterr().err


def test_open_prs_that_cannot_be_read_refuse_the_launch(tmp_path, capsys):
    _held_elsewhere(tmp_path, (ALERTS,))
    tools, run = fake_tools(
        answers={"daniel-box": ok(HEADROOM)}, issues=_citing(ALERTS), open_prs=None
    )
    assert _launch(tools, tmp_path, "--host", "daniel-box", "--batch", "1") == 1
    assert not run.calls
    assert "could not read the open PRs" in capsys.readouterr().err


def test_allow_shared_file_cannot_excuse_the_sops_file_another_run_holds(
    tmp_path, capsys
):
    # fact: .claude/skills/issue-fanout/SKILL.md#1. Triage
    _held_elsewhere(tmp_path, (SOPS,))
    tools, run = fake_tools(
        answers={"daniel-box": ok(HEADROOM)},
        issues=_citing(SOPS),
        open_prs={HELD_BRANCH},
    )
    argv = ("--host", "daniel-box", "--batch", "1", "--allow-shared-file", SOPS)
    assert _launch(tools, tmp_path, *argv) == 1
    assert not run.calls
    assert "--allow-shared-file cannot excuse this path" in capsys.readouterr().err


def test_an_excused_file_is_recorded_on_the_batch_that_was_let_through(tmp_path):
    _held_elsewhere(tmp_path, (ALERTS,))
    tools, _ = fake_tools(
        answers={"daniel-box": ok(HEADROOM)},
        issues=_citing(ALERTS),
        open_prs={HELD_BRANCH},
    )
    argv = ("--host", "daniel-box", "--batch", "1", "--allow-shared-file", ALERTS)
    assert _launch(tools, tmp_path, *argv) == 0
    new_run = max(tmp_path.glob("*.json"))
    (batch,) = json.loads(new_run.read_text())["batches"]
    assert (batch["cited_paths"], batch["excused_paths"]) == ([ALERTS], [ALERTS])
