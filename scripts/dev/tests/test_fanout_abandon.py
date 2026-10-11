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

from fanout_lib.abandon import remote_abandon_command
from fanout_lib.manifest import Batch, Manifest, path as manifest_path, save
from fanout_lib.place import main
from lib.git_testing import git, init_repo, scrubbed_env
from lib.proc_testing import fake_bin, path_with, run
from _fanout_fakes import fake_tools, ok

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


def _run_chain(tmp_path, repo, worktree, unit_active=False):
    """Run the chain under bash with a `systemctl` stub, so no real user manager is reached."""
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
    tools, calls = fake_tools(answers={"daniel-box": ok("removed: /w1 (abandoned)")})
    assert _abandon(tools, tmp_path, "1-2") == 0
    written = json.loads(manifest_path(RUN_ID, tmp_path).read_text())
    assert [b["removed_at"] is not None for b in written["batches"]] == [True, False]
    assert calls.calls[-1] == (
        "findings",
        "release 1 2 --worktree worktree-orch --reason fan-out batch 1-2 abandoned",
        None,
    )


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
    out = capsys.readouterr().out
    assert "run: findings.py release 3 --worktree worktree-orch --reason" in out


def _findings_answering(tools, returncode, stdout):
    def findings(argv):
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="")

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
    assert "release failed (3)" in capsys.readouterr().out


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
        proc = _run_chain(tmp_path, repo, worktree)
    finally:
        holder.kill()
        holder.wait()
    assert proc.stdout.strip() == f"kept: {worktree} — pid {holder.pid} still uses it"
    branches, registrations = _state(repo)
    assert BRANCH in branches and str(worktree) in registrations
