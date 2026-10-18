"""The launch gates: what `launch` refuses before it spends an ssh connection.

Run: uv run pytest scripts/dev/tests/test_fanout_launch_gates.py
"""

import json
import socket

from fanout_lib.brief import Issue
from fanout_lib.manifest import Batch, Manifest, save
from fanout_lib.place import main
from _fanout_fakes import HOST_KEY, fake_tools, ok

# Roomy on both planes and holding a registered key, so a refusal here comes from a gate
# rather than from placement or the signing gate.
HEADROOM = f"1\n12884901888\n1\n12884901888\n0\n{HOST_KEY}\n"
CLAIMED = [
    Issue(1, "one", "body one", ("claude",)),
    Issue(2, "two", "body two", ("claude",)),
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
    earlier = [Batch(f"{n}", remote, "/w", "b", "u", [n], "t") for n in (91, 92, 93)]
    save(Manifest("20260101T000010Z", "o", earlier), root=tmp_path)
    tools, run = fake_tools(answers={remote: ok(HEADROOM)}, issues=CLAIMED)
    assert _launch(tools, tmp_path, "--host", remote, "--batch", "1,2") == 3
    err = capsys.readouterr().err
    assert f"{remote} already holds 3 live batch(es)" in err
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


def _three_uncleaned(tmp_path, host):
    """Three uncleaned batches on `host`, whose unit states the headroom read can carry."""
    earlier = [Batch(f"{n}", host, "/w", "b", "u", [n], "t") for n in (91, 92, 93)]
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
    """#5497 and #5506: 3 uncleaned batches, 2 finished, so 1 running and the launch places.

    Run on this host and on the other, because only the other costs an ssh connection.
    """
    for host in (socket.gethostname(), _another_host()):
        _three_uncleaned(tmp_path, host)
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
    remote = _another_host()
    _three_uncleaned(tmp_path, remote)
    reply = _headroom_with(*(_unit_state(str(n), running=False) for n in (91, 92, 93)))
    code, run = _launch_pinned(tmp_path, remote, reply)
    assert code == 0
    assert len(run.host_calls) == 3  # headroom read, health read, one launch
    headroom, *rest = run.host_calls
    assert "systemctl --user show" in headroom[1]
    assert not any("systemctl --user show" in command for _, command, _ in rest)


def test_three_running_batches_still_refuse_the_launch(tmp_path, capsys):
    remote = _another_host()
    _three_uncleaned(tmp_path, remote)
    reply = _headroom_with(*(_unit_state(str(n), running=True) for n in (91, 92, 93)))
    code, run = _launch_pinned(tmp_path, remote, reply)
    assert code == 3
    assert f"{remote} already holds 3 live batch(es)" in capsys.readouterr().err
    assert len(run.host_calls) == 1


def test_a_unit_state_that_cannot_be_read_counts_as_running(tmp_path, capsys):
    """Fail closed: no marker, an empty section and a missing block each leave the batch counted."""
    remote = _another_host()
    replies = (
        ok(HEADROOM),
        _headroom_with(),
        # One finished batch, and no block at all for the other two.
        _headroom_with(_unit_state("91", running=False)),
    )
    for reply in replies:
        _three_uncleaned(tmp_path, remote)
        # Two new batches: the 2 or 3 live ones plus 2 exceed the cap of 3, and would not if
        # an unreadable state counted as stopped.
        code, _ = _launch_pinned(tmp_path, remote, reply, batches=("1", "2"))
        assert code == 3
        assert "live batch(es)" in capsys.readouterr().err
        _clear(tmp_path)


def test_a_dotfiles_launch_runs_here_and_reads_the_dotfiles_register(tmp_path):
    here = socket.gethostname()
    issue = Issue(763, "dotfiles finding", "body", ("claude",))
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
    issue = Issue(763, "dotfiles finding", "body", ("claude",))
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
