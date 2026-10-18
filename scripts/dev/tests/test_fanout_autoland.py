"""`fanout.py land-server` (#5496): which finished daniel-server batches land unattended.

The review record comes over the transport fake (`FakeRun`), the open PRs from a table, and the
landing's start from a recorder, so a test reads what the command decided and what it started.

Run: uv run pytest scripts/dev/tests/test_fanout_autoland.py
"""

import json
import subprocess
from pathlib import Path

import pytest
from _fanout_fakes import fake_tools
from _review_fakes import PR, _finding, _pipeline, _report

from fanout_lib import autoland
from fanout_lib.autoland import Boundaries

HEAD = "a" * 40
CLEAN_BODY = (
    "## What changed, and why\n\nFixes it.\n\n## Deploy\n- [ ] Deployed after merge\n"
)


def _record(number=5001, **over):
    rec = {
        "file": f"{number}-20261018T010000Z.json",
        "batch": str(number),
        "pr": f"https://github.com/DanielH2018/server/pull/{number}",
        "outcome": "pr",
        "review_error": "",
        "reviewed": True,
        "ci_gate": "passed",
        "remaining": 0,
        "head": HEAD,
    }
    return {**rec, **over}


def _pr(number=5001, **over):
    pr = {
        "number": number,
        "isDraft": False,
        "isCrossRepository": False,
        "headRefName": f"worktree-fanout-{number}",
        "headRefOid": HEAD,
        "baseRefName": "master",
        "author": {"login": "DanielH2018"},
        "body": CLEAN_BODY,
        "files": [{"path": "ansible/roles/k8s/sonarr/defaults/main.yml"}],
    }
    return {**pr, **over}


class _Landings:
    """The boundaries a run crosses, recorded."""

    def __init__(self, records, prs, start_rc=0, ssh=None):
        answer = ssh or subprocess.CompletedProcess([], 0, json.dumps(records), "")
        self.tools, self.ssh = fake_tools(answers={autoland.SERVER_HOST: answer})
        self.started, self.released, self.prepared = [], [], []

        def prepare(primary, state, number):
            self.prepared.append(number)
            return state / "checkouts" / f"pr{number}"

        def start(checkout, number, log_dir):
            self.started.append((number, checkout))
            return subprocess.CompletedProcess([], start_rc, "", "land: refused: boom")

        self.boundaries = Boundaries(
            tools=self.tools,
            open_prs=lambda: prs,
            prepare=prepare,
            start=start,
            release=lambda primary, path: self.released.append(path),
            running=lambda number: False,
        )
        self.lines = []

    def run(self, tmp_path, **kw):
        rc = autoland.run(
            self.boundaries,
            tmp_path / "primary",
            tmp_path / "state",
            out=self.lines.append,
            **kw,
        )
        return rc, "\n".join(self.lines)


def test_an_eligible_batch_hands_its_pr_to_land_sh_from_a_fresh_checkout(tmp_path):
    run = _Landings([_record()], [_pr()])
    rc, text = run.run(tmp_path)
    assert rc == 0
    assert run.started == [(5001, tmp_path / "state" / "checkouts" / "pr5001")]
    assert "#5001 batch 5001: eligible, handed off to land.sh" in text


@pytest.mark.parametrize(
    ("rec", "reason"),
    [
        ({"remaining": 2}, "2 findings remain after the fix round"),
        ({"reviewed": False}, "the review did not complete"),
        (
            {"review_error": "the review session failed (x)"},
            "the review did not complete",
        ),
        ({"outcome": "needs-input"}, "the batch ended 'needs-input'"),
        ({"ci_gate": "no verdict: WAIT: pending"}, "the CI gate did not pass"),
        ({"head": ""}, "the record names no reviewed head"),
    ],
)
def test_a_record_that_does_not_show_a_clean_review_stops_at_the_pr(
    tmp_path, rec, reason
):
    run = _Landings([_record(**rec)], [_pr()])
    _, text = run.run(tmp_path)
    assert run.started == []
    assert f"ineligible, {reason}" in text


def test_a_no_review_batch_leaves_no_record_and_never_lands(tmp_path):
    # `--no-review` runs no pipeline, so nothing writes a record; the open PR is not a candidate.
    run = _Landings([], [_pr()])
    _, text = run.run(tmp_path)
    assert run.started == []
    assert "0 still open" in text


def test_an_unchecked_operator_box_never_lands_but_the_templates_deploy_boxes_do(
    tmp_path,
):
    held = "## Before merge\n- [ ] Operator ran the dry run\n"
    run = _Landings(
        [_record(5001), _record(5002)],
        [_pr(5001, body=held), _pr(5002, body=CLEAN_BODY)],
    )
    _, text = run.run(tmp_path)
    assert [n for n, _ in run.started] == [5002]
    assert "ineligible, an unchecked box under 'Before merge'" in text


@pytest.mark.parametrize("phrase", ["needs input: the reviewer", "DO NOT MERGE yet"])
def test_text_saying_the_pr_needs_input_holds_it(tmp_path, phrase):
    run = _Landings([_record()], [_pr(body=f"{CLEAN_BODY}\n{phrase}")])
    run.run(tmp_path)
    assert run.started == []


def test_a_head_that_moved_since_the_review_never_lands(tmp_path):
    run = _Landings([_record()], [_pr(headRefOid="b" * 40)])
    _, text = run.run(tmp_path)
    assert run.started == []
    assert "the head moved since the review (aaaaaaaa -> bbbbbbbb)" in text


@pytest.mark.parametrize(
    "over",
    [
        {"isDraft": True},
        {"headRefName": "worktree-fanout-9999"},
        {"baseRefName": "other"},
        {"author": {"login": "someone"}},
        {"files": [{"path": "scripts/deploy_tools/land_lib/policy.py"}]},
        {"files": [{"path": "ansible/roles/setup/claude_code/tasks/main.yml"}]},
    ],
)
def test_a_pr_that_is_not_the_batchs_own_clean_pr_never_lands(tmp_path, over):
    run = _Landings([_record()], [_pr(**over)])
    run.run(tmp_path)
    assert run.started == []


def test_a_failed_handoff_is_logged_with_its_reason_and_starts_nothing_else(tmp_path):
    run = _Landings([_record()], [_pr()], start_rc=1)
    _, text = run.run(tmp_path)
    assert "eligible, handoff failed (exit 1): land: refused: boom" in text
    # The only calls past the start are the checkout's removal: nothing touched the PR.
    assert run.released == [tmp_path / "state" / "checkouts" / "pr5001"]


def test_a_checkout_that_cannot_be_made_is_logged_and_nothing_starts(tmp_path):
    run = _Landings([_record()], [_pr()])

    def refuse(primary, state, number):
        raise autoland.CheckoutFailed("fatal: no origin")

    run.boundaries.prepare = refuse
    _, text = run.run(tmp_path)
    assert run.started == []
    assert "handoff failed, no checkout: fatal: no origin" in text


def test_the_run_reads_daniel_server_over_exactly_one_ssh_call(tmp_path):
    batch = [_record(n) for n in (5001, 5002, 5003, 5004)]
    run = _Landings(batch, [_pr(n) for n in (5001, 5002, 5003, 5004)])
    run.run(tmp_path)
    hosts = [host for host, *_ in run.ssh.host_calls]
    assert hosts == [autoland.SERVER_HOST]
    assert run.ssh.host_calls[0][1] == "python3 -I -"
    assert run.ssh.host_calls[0][2] == autoland.READER


def test_at_most_two_landings_start_per_run(tmp_path):
    numbers = (5001, 5002, 5003)
    run = _Landings([_record(n) for n in numbers], [_pr(n) for n in numbers])
    _, text = run.run(tmp_path)
    assert [n for n, _ in run.started] == [5001, 5002]
    assert "#5003 batch 5003: eligible, deferred to the next run" in text


def test_a_pr_this_command_already_handed_off_is_not_started_again(tmp_path):
    logs = tmp_path / "state" / "logs"
    logs.mkdir(parents=True)
    (logs / "land5001-20261018-010000.log").write_text("VERDICT: refused\n")
    run = _Landings([_record()], [_pr()])
    _, text = run.run(tmp_path)
    assert run.started == []
    assert "already handed off (land5001-20261018-010000.log)" in text


def test_a_landing_already_running_elsewhere_is_not_started_again(tmp_path):
    run = _Landings([_record()], [_pr()])
    run.boundaries.running = lambda number: True
    _, text = run.run(tmp_path)
    assert run.started == []
    assert "a landing of it is already running" in text


def test_the_process_scan_compares_whole_tokens(tmp_path):
    def proc(pid, *argv):
        d = tmp_path / str(pid)
        d.mkdir()
        (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")

    proc(100, "python", "scripts/deploy_tools/land.py", "--pr", "5501", "--arm-merge")
    assert autoland.landing_running(5501, tmp_path)
    assert not autoland.landing_running(55, tmp_path)
    assert not autoland.landing_running(5502, tmp_path)


def test_an_unreadable_daniel_server_is_a_logged_line_and_exit_zero(tmp_path):
    down = subprocess.CompletedProcess([], 255, "", "ssh: connect: refused")
    run = _Landings([], [_pr()], ssh=down)
    rc, text = run.run(tmp_path)
    assert rc == 0 and run.started == []
    assert "daniel-server records unreadable, nothing landed: exit 255" in text


def test_an_unreadable_github_exits_one_and_starts_nothing(tmp_path):
    run = _Landings([_record()], [])

    def broken():
        raise subprocess.CalledProcessError(1, ["gh"], stderr="bad credentials")

    run.boundaries.open_prs = broken
    rc, text = run.run(tmp_path)
    assert rc == 1 and run.started == []
    assert "GitHub unreadable" in text


def test_a_dry_run_judges_and_starts_nothing(tmp_path):
    run = _Landings([_record()], [_pr()])
    _, text = run.run(tmp_path, dry_run=True)
    assert run.started == [] and run.prepared == []
    assert "eligible, would hand off to land.sh (dry run)" in text


def test_the_newest_record_of_a_pr_decides(tmp_path):
    old = _record(file="5001-20261017T010000Z.json", remaining=3)
    new = _record(file="5001-20261018T010000Z.json")
    assert autoland.newest_per_pr([new, old])[5001] is new
    assert autoland.newest_per_pr([old, new])[5001] is new


def test_the_timer_command_is_a_fanout_subcommand():
    assert Path(autoland.__file__).name == "autoland.py"
    from fanout import COMMANDS

    assert COMMANDS["land-server"] == "fanout_lib.autoland"


def test_the_record_names_the_head_the_last_reviewer_read(tmp_path):
    """`fanout_lib.autoland` lands a PR only at this head (#5496)."""
    clean = [
        _report(f"Opened {PR}"),
        _report(structured={"summary": "", "findings": []}),
    ]
    (tmp_path / "first").mkdir()
    pipeline, _run = _pipeline(tmp_path / "first", clean)
    pipeline.run_all()
    (first,) = (tmp_path / "first" / "state").glob("1345-*.json")
    assert json.loads(first.read_text())["head"] == "aaa"

    fixed = [
        _report(f"Opened {PR}"),
        _report(structured={"summary": "", "findings": [_finding("off by one")]}),
        _report(f"Fixed it. {PR}"),
        _report(structured={"summary": "resolved", "findings": []}),
    ]
    (tmp_path / "second").mkdir()
    pipeline, _run = _pipeline(tmp_path / "second", fixed)
    pipeline.run_all()
    (second,) = (tmp_path / "second" / "state").glob("1345-*.json")
    assert json.loads(second.read_text())["head"] == "bbb"
