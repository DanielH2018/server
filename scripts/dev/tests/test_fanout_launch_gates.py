"""The launch gates: what `launch` refuses before it spends an ssh connection.

Run: uv run pytest scripts/dev/tests/test_fanout_launch_gates.py
"""

import json
import socket

from fanout_lib.brief import Issue
from fanout_lib.manifest import Batch, Manifest, save
from fanout_place import main
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
    """Headroom alone underprices agents launched minutes ago, so count them."""
    earlier = [
        Batch(f"{n}", "daniel-box", "/w", "b", "u", [n], "t") for n in (91, 92, 93)
    ]
    save(Manifest("20260101T000010Z", "o", earlier), root=tmp_path)
    tools, run = fake_tools(answers={"daniel-box": ok(HEADROOM)}, issues=CLAIMED)
    assert _launch(tools, tmp_path, "--host", "daniel-box", "--batch", "1,2") == 3
    err = capsys.readouterr().err
    assert "daniel-box already holds 3 live batch(es)" in err
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
    assert _launch(tools, tmp_path, *argv) == 1
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
