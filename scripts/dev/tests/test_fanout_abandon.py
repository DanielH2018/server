"""`fanout.py place abandon`: one unmerged batch's tree, branch and claims, gone in one call.

The chain tests EXECUTE `remote_abandon_command` against a scratch repo, the way
test_fanout_clean_chain.py does, because the order of `unlock`, `remove` and `branch -D` is
what fails, and only running it shows that. The CLI tests drive `main` over a saved manifest
with fake tools.

Run: uv run pytest scripts/dev/tests/test_fanout_abandon.py
"""

import dataclasses
import json
import subprocess
from functools import partial
from unittest import mock

import pytest

from dev.fanout_lib import abandon
from dev.fanout_lib.abandon import remote_abandon_command
from dev.fanout_lib.clean import live_process_scan
from dev.fanout_lib.manifest import (
    Batch,
    Manifest,
    load,
    path as manifest_path,
    retired_path,
    save,
)
from dev.fanout_lib.place import main
from lib.git_testing import git, init_repo, scrubbed_env
from lib.proc_testing import fake_bin, path_with, run
from _fanout_fakes import HOST_KEY, fake_tools, ok

BRANCH = "worktree-fanout-x"
UNIT = "fanout-x"
RUN_ID = "20260101T000010Z"


def _scratch_with_an_unmerged_locked_worktree(tmp_path):
    """A batch tree as `launch` leaves it: locked, holding a commit that never landed."""
    repo = init_repo(tmp_path / "repo", initial_commit="init")
    worktree = tmp_path / "w"
    git(repo, "worktree", "add", "-q", "-b", BRANCH, str(worktree))
    git(worktree, "commit", "-q", "-m", "work", "--allow-empty", "--no-gpg-sign")
    # What an abandoned tree holds: uncommitted edits, `.fanout/`, an ignored venv.
    (worktree / ".gitignore").write_text(".venv/\n")
    (worktree / ".fanout").mkdir()
    (worktree / ".fanout" / "report.json").write_text("{}")
    (worktree / ".venv").mkdir()
    git(repo, "worktree", "lock", "--reason", UNIT, str(worktree))
    return repo, worktree


def _run_chain(tmp_path, repo, worktree, unit_active=False, proc_root=None):
    """Run the chain under bash with a `systemctl` stub, so no real user manager is reached.

    `proc_root` is the directory the chain's in-use scan walks. It defaults to an empty one,
    because the scan forks per pid and the host's process count then sets the test's cost.
    Only a test that needs the scan to find a live process passes `/proc`.
    """
    if proc_root is None:
        proc_root = tmp_path / "proc"
        proc_root.mkdir()
    bin_dir = tmp_path / "bin"
    rc = 0 if unit_active else 3
    fake_bin(
        bin_dir,
        systemctl=(
            f'#!/bin/sh\necho "$@" >> {bin_dir / "systemctl-calls"}\n'
            f'case "$2" in is-active) exit {rc} ;; esac\nexit 0\n'
        ),
    )
    batch = Batch("x", "h", str(worktree), BRANCH, UNIT, [1], "t")
    # `remote_abandon_command` takes no proc-root seam, so the scan it calls is wrapped here.
    scan = partial(live_process_scan, proc_root=str(proc_root))
    with mock.patch.object(abandon, "live_process_scan", scan):
        cmd = remote_abandon_command(batch, repo=str(repo))
    assert str(repo) in cmd and "/home/ubuntu/server" not in cmd
    env = scrubbed_env()
    env["PATH"] = path_with(str(bin_dir), env=env)
    return run(["bash", "-c", cmd], env=env)


def _state(repo):
    branches = git(repo, "branch", "--list").stdout
    registrations = git(repo, "worktree", "list", "--porcelain").stdout
    return branches, registrations


def test_an_unmerged_dirty_locked_tree_and_its_branch_are_removed(tmp_path):
    repo, worktree = _scratch_with_an_unmerged_locked_worktree(tmp_path)
    proc = _run_chain(tmp_path, repo, worktree)
    assert proc.stdout.strip() == f"removed: {worktree} (abandoned)"
    calls = (tmp_path / "bin" / "systemctl-calls").read_text().splitlines()
    assert calls[0] == f"--user stop {UNIT}"
    branches, registrations = _state(repo)
    assert BRANCH not in branches
    assert str(worktree) not in registrations
    assert not worktree.exists()


def test_a_unit_that_survives_the_stop_keeps_the_tree(tmp_path):
    repo, worktree = _scratch_with_an_unmerged_locked_worktree(tmp_path)
    proc = _run_chain(tmp_path, repo, worktree, unit_active=True)
    assert (
        proc.stdout.strip() == f"kept: {worktree} — unit {UNIT} still active after stop"
    )
    branches, registrations = _state(repo)
    assert BRANCH in branches and str(worktree) in registrations


def test_a_stub_left_where_the_tree_was_is_cleared_with_the_branch(tmp_path):
    """An agent that removed its own tree leaves a `.remember/` stub that is not a checkout."""
    repo, worktree = _scratch_with_an_unmerged_locked_worktree(tmp_path)
    git(repo, "worktree", "unlock", str(worktree))
    git(repo, "worktree", "remove", "--force", str(worktree))
    (worktree / ".remember").mkdir(parents=True)
    proc = _run_chain(tmp_path, repo, worktree)
    assert proc.stdout.strip() == f"removed: {worktree} (abandoned)"
    assert BRANCH not in _state(repo)[0]
    assert not worktree.exists()


B1 = Batch("1-2", "daniel-box", "/w1", "worktree-fanout-1-2", "fanout-1-2", [1, 2], "t")
B2 = Batch("3", "daniel-box", "/w3", "worktree-fanout-3", "fanout-3", [3], "t")


def _abandon(tools, tmp_path, batch):
    save(Manifest(RUN_ID, "worktree-orch", [B1, B2]), root=tmp_path)
    return main(["abandon", RUN_ID, batch, "--manifest-root", str(tmp_path)], tools)


def test_abandon_records_the_removal_and_releases_under_the_orchestrator(tmp_path):
    # fact: .claude/skills/issue-fanout/SKILL.md#5. Report
    tools, calls = fake_tools(answers={"daniel-box": ok("removed: /w1 (abandoned)")})
    assert _abandon(tools, tmp_path, "1-2") == 0
    written = json.loads(manifest_path(RUN_ID, tmp_path).read_text())
    assert [b["removed_at"] is not None for b in written["batches"]] == [True, False]
    assert calls.calls[-1] == (
        "findings",
        "release 1 2 --worktree worktree-orch --reason fan-out batch 1-2 abandoned",
        None,
    )


def test_abandoning_the_last_batch_retires_the_manifest_with_its_removal(tmp_path):
    """The retired copy carries the last `removed_at`, so `status` reads the run as cleaned."""
    save(Manifest(RUN_ID, "worktree-orch", [B1]), root=tmp_path)
    tools, _calls = fake_tools(answers={"daniel-box": ok("removed: /w1 (abandoned)")})
    assert (
        main(["abandon", RUN_ID, "1-2", "--manifest-root", str(tmp_path)], tools) == 0
    )
    assert not manifest_path(RUN_ID, tmp_path).exists()
    written = json.loads(retired_path(RUN_ID, tmp_path).read_text())
    assert [b["removed_at"] is not None for b in written["batches"]] == [True]


def test_abandon_of_a_kept_tree_releases_nothing_and_exits_1(tmp_path):
    kept = ok("kept: /w1 — unit fanout-1-2 still active after stop")
    tools, calls = fake_tools(answers={"daniel-box": kept})
    assert _abandon(tools, tmp_path, "1-2") == 1
    written = json.loads(manifest_path(RUN_ID, tmp_path).read_text())
    assert written["batches"][0]["removed_at"] is None
    assert not [c for c in calls.calls if c[0] == "findings"]


def test_a_failed_release_prints_the_command_to_retry(tmp_path, capsys):
    tools, _calls = fake_tools(
        answers={"daniel-box": ok("removed: /w3 (abandoned)")}, findings_exit=1
    )
    assert _abandon(tools, tmp_path, "3") == 1
    out = capsys.readouterr().err
    assert "run: findings.py release 3 --worktree worktree-orch --reason" in out


def _findings_answering(tools, returncode, stderr):
    def findings(argv):
        return subprocess.CompletedProcess(argv, returncode, stdout="", stderr=stderr)

    return dataclasses.replace(tools, findings=findings)


def test_a_second_release_of_claims_already_gone_is_not_a_failure(tmp_path, capsys):
    """`stop` then `abandon` releases twice; the second finds nothing claimed."""
    tools, _calls = fake_tools(answers={"daniel-box": ok("removed: /w3 (abandoned)")})
    tools = _findings_answering(tools, 3, "#3 refused: not claimed\n")
    assert _abandon(tools, tmp_path, "3") == 0
    assert "claims on #3 already released" in capsys.readouterr().out


def test_a_release_refused_for_another_holder_is_a_failure(tmp_path, capsys):
    tools, _calls = fake_tools(answers={"daniel-box": ok("removed: /w3 (abandoned)")})
    refusal = "#3 refused: claimed by `worktree-other`, not by `worktree-orch`\n"
    tools = _findings_answering(tools, 3, refusal)
    assert _abandon(tools, tmp_path, "3") == 1
    assert "release failed (3)" in capsys.readouterr().err


def test_abandon_names_the_batches_when_the_batch_is_unknown(tmp_path, capsys):
    tools, calls = fake_tools()
    assert _abandon(tools, tmp_path, "9") == 1
    assert "has no batch 9 (it has 1-2, 3)" in capsys.readouterr().err
    assert not calls.calls


def test_the_skill_has_no_hand_run_abandon_teardown():
    from pathlib import Path

    skill = Path(__file__).resolve().parents[3] / ".claude/skills/issue-fanout/SKILL.md"
    text = skill.read_text()
    assert "fanout.py place abandon" in text
    assert "git branch -D worktree-fanout" not in text


def test_a_tree_a_live_process_still_uses_is_kept(tmp_path):
    """A shell `cd`'d into the tree is not the batch's unit, so stopping the unit missed it."""
    repo, worktree = _scratch_with_an_unmerged_locked_worktree(tmp_path)
    holder = subprocess.Popen(["sleep", "60"], cwd=worktree)
    try:
        proc = _run_chain(tmp_path, repo, worktree, proc_root="/proc")
    finally:
        holder.kill()
        holder.wait()
    assert proc.stdout.strip() == f"kept: {worktree} — pid {holder.pid} still uses it"
    branches, registrations = _state(repo)
    assert BRANCH in branches and str(worktree) in registrations


# `fanout.py place resume`, over a batch that has ended on daniel-box.
RESUMED = Batch(
    "7", "daniel-box", "/w/fanout-7", "worktree-fanout-7", "fanout-7", [7], "t"
)
PR = "https://github.com/DanielH2018/server/pull/4000"
CONFLICT = "VERDICT: merge-conflict (PR #4000 conflicts with master)"
CONFLICT_LOG = (
    f"LAND-RESULT: verdict=merge-conflict cause=- pr=4000 exit=1\n{CONFLICT}\n"
)
HEADROOM = f"1\n12884901888\n1\n12884901888\n0\n{HOST_KEY}\n"


def _host_read(state="inactive", tree="standing\n", own_log=""):
    """What `resume.read_command` prints for a batch whose PR conflicts in two files."""
    return ok(
        f"=== 7\nActiveState={state}\nResult=success\nExecMainStatus=0\n"
        f"--- tree\n{tree}--- landing\n{own_log}"
        "--- conflicts\ntree0\nscripts/a.py\ndocs/b.md\nexit=1\n"
    )


def _resume(
    tmp_path, batch=RESUMED, reads=None, landing=CONFLICT_LOG, args=(), **forge
):
    """Run `place resume` on `batch`, with the coordinator's own landing log `landing`."""
    save(Manifest(RUN_ID, "worktree-orch", [batch]), root=tmp_path / "runs")
    logs = tmp_path / "landings"
    logs.mkdir()
    if landing:
        (logs / "land4000-20261120-100000.log").write_text(landing)
    forge = {"branch_prs": {batch.branch: [PR]}, "open_prs": (batch.branch,), **forge}
    tools, run = fake_tools(**forge)
    # The batch's host, the landing locks' host, the headroom read, then the unit's start.
    run.answers_by_call = reads or [_host_read(), ok("free\n"), ok(HEADROOM), ok("")]
    argv = ["resume", RUN_ID, batch.batch, "--log-dir", str(logs), *args]
    code = main([*argv, "--manifest-root", str(tmp_path / "runs")], tools)
    return code, run


def _started(run):
    return [call for call in run.host_calls if "systemd-run" in call[1]]


def test_resume_restarts_a_finished_batch_on_the_verdict_of_a_landing_run_outside_it(
    tmp_path, capsys
):
    code, run = _resume(tmp_path)
    assert code == 0
    ((host, command, request),) = _started(run)
    assert host == "daniel-box" and "--resume-conflict" in command
    assert json.loads(request) == {"pr": PR, "line": CONFLICT}
    (batch,) = load(RUN_ID, tmp_path / "runs").batches
    assert batch.conflict_resumes == 1
    assert capsys.readouterr().out == (
        f"run {RUN_ID}: resumed 7 on daniel-box for {PR} (conflict resume 1 of 2); "
        f"wait with cc-wait fanout {RUN_ID}\n"
    )


LOCK_RECORD = json.dumps(
    {"host": "daniel-box", "pid": 41, "user": "ubuntu", "log": "/tmp/land4000.log"}
)


@pytest.mark.parametrize(
    ("changed", "reason", "instead"),
    [
        (
            {"reads": [_host_read(state="active")]},
            "7 on daniel-box is still running",
            f"cc-wait fanout {RUN_ID}",
        ),
        (
            {"batch": dataclasses.replace(RESUMED, removed_at="2026-11-20")},
            "7 on daniel-box was cleaned (2026-11-20)",
            "fanout.py place launch --batch 7",
        ),
        (
            {"reads": [_host_read(tree="")]},
            "its worktree /w/fanout-7 is gone",
            f"fanout.py place clean {RUN_ID}",
        ),
        (
            {"merged_prs": {RESUMED.branch: PR}},
            f"{PR} merged",
            f"fanout.py place clean {RUN_ID}",
        ),
        (
            {"open_prs": ()},
            "worktree-fanout-7 has no open PR",
            f"fanout.py place abandon {RUN_ID} 7",
        ),
        (
            {"reads": [_host_read(), ok(f"held\n{LOCK_RECORD}\n")]},
            "a landing of #4000 is live (host daniel-box, pid 41)",
            "wait with `cc-wait land 4000 --log /tmp/land4000.log`, then run this again",
        ),
        (
            {"landing": "LAND-RESULT: verdict=pr-ci-red\nVERDICT: pr-ci-red (x)\n"},
            "ended `pr-ci-red`, not `merge-conflict`",
            f"fanout.py place status {RUN_ID}",
        ),
        (
            {"landing": ""},
            "found no landing log for #4000",
            "name it with --log <path>, or land the PR with `land.sh --pr 4000 ",
        ),
    ],
    ids=[
        "running",
        "cleaned",
        "no-worktree",
        "merged",
        "closed",
        "landing-live",
        "red",
        "no-log",
    ],
)
def test_resume_refuses_with_its_reason_and_the_command_that_fits(
    tmp_path, capsys, changed, reason, instead
):
    code, run = _resume(tmp_path, **changed)
    assert code == 1 and not _started(run)
    err = capsys.readouterr().err
    assert reason in err and instead in err


def test_the_batchs_own_landing_decides_when_it_is_newer_than_a_sessions(tmp_path):
    # A session's `land.sh` ended red, then the batch's own landing conflicted.
    own = f"/w/fanout-7/.fanout/land4000-20261120-110000.log\n{CONFLICT_LOG}"
    red = "LAND-RESULT: verdict=pr-ci-red\nVERDICT: pr-ci-red (x)\n"
    reads = [_host_read(own_log=own), ok("free\n"), ok(HEADROOM), ok("")]
    code, run = _resume(tmp_path, reads=reads, landing=red)
    assert code == 0 and len(_started(run)) == 1


def test_a_log_named_with_the_flag_is_the_only_landing_read(tmp_path):
    elsewhere = tmp_path / "land4000-20261119-080000.log"
    elsewhere.write_text(CONFLICT_LOG)
    settled = "LAND-RESULT: verdict=settled\nVERDICT: settled (deployed)\n"
    code, run = _resume(tmp_path, landing=settled, args=("--log", str(elsewhere)))
    assert code == 0 and len(_started(run)) == 1


def test_a_unit_that_fails_to_start_spends_no_resume(tmp_path, capsys):
    refused = subprocess.CompletedProcess(
        ["x"], 1, "", "Failed to start transient unit\nfanout-step: systemd-run\n"
    )
    reads = [_host_read(), ok("free\n"), ok(HEADROOM), refused]
    code, _ = _resume(tmp_path, reads=reads)
    assert code == 1
    assert "7 on daniel-box: systemd-run failed (1)" in capsys.readouterr().err
    assert load(RUN_ID, tmp_path / "runs").batches[0].conflict_resumes == 0


def test_resume_past_the_limit_refuses_and_names_the_conflicting_files(
    tmp_path, capsys
):
    spent = RESUMED.resumed_for_conflict().resumed_for_conflict()
    code, run = _resume(tmp_path, batch=spent)
    assert code == 1 and not _started(run)
    err = capsys.readouterr().err
    assert "was resumed 2 times for a conflict, which is the limit" in err
    assert "conflicts with origin/master in: scripts/a.py, docs/b.md" in err
