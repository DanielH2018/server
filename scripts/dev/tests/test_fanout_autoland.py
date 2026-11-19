"""`fanout.py land-server`: which finished daniel-server batches land unattended.

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

from deploy_tools.land_lib import pr_lock
from deploy_tools.land_lib.ledger import Ledger, result_line
from deploy_tools.land_lib.outcome import Cause, Outcome, Verdict
from fanout_lib import autoland, own_surface
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
        "files": [{"path": "k8s/roles/sonarr/defaults/main.yml"}],
    }
    return {**pr, **over}


class _Landings:
    """The boundaries a run crosses, recorded."""

    def __init__(self, records, prs, start_rc=0, ssh=None, start_out=""):
        answer = ssh or subprocess.CompletedProcess([], 0, json.dumps(records), "")
        self.tools, self.ssh = fake_tools(answers={autoland.SERVER_HOST: answer})
        self.started, self.released, self.prepared, self.swept = [], [], [], []

        def prepare(primary, state, number):
            self.prepared.append(number)
            return state / "checkouts" / f"pr{number}"

        def start(checkout, number, log_dir):
            self.started.append((number, checkout))
            return subprocess.CompletedProcess(
                [], start_rc, start_out, "" if start_out else "land: refused: boom"
            )

        self.boundaries = Boundaries(
            tools=self.tools,
            open_prs=lambda: prs,
            prepare=prepare,
            start=start,
            release=lambda primary, path: self.released.append(path),
            running=lambda number: False,
            clean=lambda tools, open_branches, out: self.swept.append(open_branches),
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
        {"files": [{"path": "hosts/components/claude-code/component.yml"}]},
    ],
)
def test_a_pr_that_is_not_the_batchs_own_clean_pr_never_lands(tmp_path, over):
    run = _Landings([_record()], [_pr(**over)])
    run.run(tmp_path)
    assert run.started == []


@pytest.mark.parametrize(
    "path",
    [
        "scripts/deploy_tools/await_ci.py",
        "scripts/lib/git.py",
        "scripts/dev/fanout_lib/autoland.py",
        ".github/workflows/ci.yml",
        ".claude/settings.json",
        "hosts/components/claude-code/component.yml",
        "secrets/secrets.yml",
        "uv.lock",
    ],
)
def test_a_pr_touching_the_landing_paths_own_surface_is_refused_with_that_reason(
    tmp_path, path
):
    run = _Landings([_record()], [_pr(files=[{"path": path}])])
    _, text = run.run(tmp_path)
    assert run.started == []
    assert "ineligible, it changes the lander's own surface" in text


PYPROJECT = """\
[project]
dependencies = ["pyyaml"]

[tool.pytest.ini_options]
testpaths = ["scripts"]
addopts = "-n auto"
"""


def _served(old, new):
    """The reader a run uses for the merge base's `pyproject.toml` and the head's."""
    return lambda head: (old, new)


@pytest.mark.parametrize(
    ("new", "lands"),
    [
        (PYPROJECT.replace('["scripts"]', '["scripts", "hosts/new/tests"]'), True),
        (PYPROJECT.replace('"pyyaml"', '"pyyaml", "requests"'), False),
        (PYPROJECT.replace("-n auto", "-n 4"), False),
        (PYPROJECT + "\n[tool.uv]\npackage = false\n", False),
        (PYPROJECT.replace('["scripts"]', "[1]"), False),
    ],
    ids=[
        "testpaths",
        "dependency",
        "other-key-in-table",
        "other-table",
        "bad-testpaths",
    ],
)
def test_pyproject_is_judged_by_what_changed_in_it(new, lands):
    assert own_surface.testpaths_only(HEAD, _served(PYPROJECT, new)) is lands


def test_a_pyproject_that_cannot_be_read_or_parsed_holds():
    assert own_surface.testpaths_only(HEAD, _served(PYPROJECT, "not [ toml")) is False

    def unreachable(head):
        raise subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 502")

    assert own_surface.testpaths_only(HEAD, unreachable) is False


@pytest.mark.parametrize(
    ("files", "lands"),
    [
        (["pyproject.toml", "scripts/dev/tests/test_x.py"], True),
        (["pyproject.toml", "uv.lock"], False),
        ([".claude/skills/deploy/SKILL.md"], False),
        ([".claude/hooks/guard.py"], False),
    ],
)
def test_the_narrowed_pyproject_hold_passes_only_its_own_diff(tmp_path, files, lands):
    run = _Landings([_record()], [_pr(files=[{"path": f} for f in files])])
    run.boundaries.testpaths_only = lambda head: True
    run.run(tmp_path)
    assert bool(run.started) is lands


def test_a_pyproject_change_that_is_not_testpaths_only_holds_with_that_reason(tmp_path):
    run = _Landings([_record()], [_pr(files=[{"path": "pyproject.toml"}])])
    run.boundaries.testpaths_only = lambda head: False
    _, text = run.run(tmp_path)
    assert run.started == []
    assert "ineligible, it changes the lander's own surface: pyproject.toml" in text


def test_a_start_refused_for_a_live_landing_is_logged_as_another_landing_has_it(
    tmp_path,
):
    ledger = Ledger(pr="5001", t_start=0.0)
    ledger.cause = Cause.LANDING_LIVE
    holder = pr_lock.Holder("daniel-server", "41", "ubuntu", "/tmp/land5001-1.log")
    refused = Outcome(75, holder.refusal("5001", "daniel-box"), Verdict.REFUSED)
    said = f"{result_line(ledger, refused)}\nVERDICT: refused ({refused.detail})\n"

    run = _Landings([_record()], [_pr()], start_rc=75, start_out=said)
    _, text = run.run(tmp_path)

    assert (
        "#5001 batch 5001: eligible, another landing has it: VERDICT: refused" in text
    )
    assert "handoff failed" not in text
    assert run.released == [tmp_path / "state" / "checkouts" / "pr5001"]


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


def test_every_eligible_pr_starts_in_one_run(tmp_path):
    numbers = (5001, 5002, 5003)
    run = _Landings([_record(n) for n in numbers], [_pr(n) for n in numbers])
    _, text = run.run(tmp_path)
    assert [n for n, _ in run.started] == [5001, 5002, 5003]
    assert "deferred" not in text


UV_CHATTER = (
    "warning: `VIRTUAL_ENV=/x/.venv` does not match the project environment path `.venv` "
    "and will be ignored; use `--active` to target the active environment instead\n"
    "Using CPython 3.15.2\nCreating virtual environment at: .venv\n"
    "Installed 50 packages in 231ms\n"
)


@pytest.mark.parametrize(
    ("stderr", "reason"),
    [
        (
            UV_CHATTER + "land: the landing turn is unreadable; re-run this command\n",
            "(exit 75): land: the landing turn is unreadable; re-run this command",
        ),
        (
            UV_CHATTER
            + "land: a warning carried past\nVERDICT: refused nothing merged\n",
            "(exit 75): VERDICT: refused nothing merged",
        ),
    ],
)
def test_a_failed_handoff_logs_land_shs_deciding_line_not_uvs_chatter(
    tmp_path, stderr, reason
):
    run = _Landings([_record()], [_pr()])
    run.boundaries.start = lambda *a: subprocess.CompletedProcess([], 75, "", stderr)
    _, text = run.run(tmp_path)
    assert f"#5001 batch 5001: eligible, handoff failed {reason}" in text


def test_a_failed_handoff_with_no_land_line_logs_the_output_tail(tmp_path):
    run = _Landings([_record()], [_pr()])
    crash = UV_CHATTER + "Traceback (most recent call last):\nKeyError: 'pr'\n"
    run.boundaries.start = lambda *a: subprocess.CompletedProcess([], 1, "", crash)
    _, text = run.run(tmp_path)
    assert text.endswith("handoff failed (exit 1): " + autoland.tail(crash))


def test_a_pr_this_command_already_handed_off_is_not_started_again(tmp_path):
    logs = tmp_path / "state" / "logs"
    logs.mkdir(parents=True)
    (logs / "land5001-20261018-010000.log").write_text("VERDICT: refused\n")
    run = _Landings([_record()], [_pr()])
    _, text = run.run(tmp_path)
    assert run.started == []
    assert "already handed off (land5001-20261018-010000.log)" in text


@pytest.mark.parametrize(("code", "started"), [(75, [5001]), (1, [])])
def test_only_a_pr_a_rule_refused_before_its_merge_is_started_again(
    tmp_path, code, started
):
    logs = tmp_path / "state" / "logs"
    logs.mkdir(parents=True)
    (logs / "land5001-20261018-010000.log").write_text(
        f"LAND-RESULT: verdict=refused cause=- exit={code} pr=5001 sha=- tags=0\n"
    )
    run = _Landings([_record()], [_pr()])
    run.run(tmp_path)
    assert [n for n, _ in run.started] == started


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
    assert run.started == [] and run.prepared == [] and run.swept == []
    assert "eligible, would hand off to land.sh (dry run)" in text


def test_a_pass_sweeps_landed_worktrees_passing_over_open_pr_branches(tmp_path):
    """The lander's timer is what runs `clean` with no operator behind it."""
    run = _Landings([_record()], [_pr(5001), _pr(5002)])
    run.run(tmp_path)
    assert run.swept == [frozenset({"worktree-fanout-5001", "worktree-fanout-5002"})]


def test_the_sweep_runs_once_per_interval(tmp_path):
    run = _Landings([], [_pr()])
    now = [1_000_000.0]
    run.boundaries.clock = lambda: now[0]
    run.run(tmp_path)
    now[0] += autoland.CLEAN_INTERVAL_S - 1
    run.run(tmp_path)
    assert len(run.swept) == 1
    now[0] += 1
    run.run(tmp_path)
    assert len(run.swept) == 2


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
    """`fanout_lib.autoland` lands a PR only at this head."""
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
