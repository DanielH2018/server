"""`clean` converges across passes, and a cleaned batch stays cleaned — Ruling 30.

Split from test_fanout_clean.py, which sits at 415 of a 500-line ceiling. The behaviour
under test is a two-pass sequence, so these drive `main` over a saved manifest rather than
`clean_one` directly.

Run: uv run pytest scripts/dev/tests/test_fanout_clean_convergence.py
"""

import json
import subprocess

from fanout_lib.clean import remote_clean_command
from fanout_lib.manifest import Batch, Manifest, path as manifest_path, save
from fanout_place import main
from _fanout_fakes import fake_tools, ok

B1 = Batch("1", "daniel-box", "/w1", "worktree-fanout-1", "fanout-1", [1], "t")
B2 = Batch("2", "daniel-server", "/w2", "worktree-fanout-2", "fanout-2", [2], "t")
UNMERGED = "kept: /w2 worktree-fanout-2 not merged into origin/master"


def _manifest(tmp_path, batches):
    manifest = Manifest("20260101T000010Z", "o", batches)
    save(manifest, root=tmp_path)
    return manifest


def _clean(tools, tmp_path, run_id):
    return main(["clean", run_id, "--manifest-root", str(tmp_path)], tools)


def test_the_absent_tree_case_is_answered_before_the_interpreter_is_needed():
    """F1: the script the `uv run` leg invokes lives inside the worktree the first pass deleted."""
    cmd = remote_clean_command(B1)
    assert cmd.index("if [ ! -e /w1 ]") < cmd.index("uv run")
    # The three outcomes of a gone tree, in the order the chain tests them.
    assert cmd.count('echo "removed: /w1 (already gone)"') == 2
    assert 'echo "kept: /w1 — branch worktree-fanout-1 unmerged, tree gone"' in cmd
    assert 'echo "kept: /w1 — branch worktree-fanout-1 not deleted"' in cmd
    # Never `removed:` while the branch survives: the delete gates the echo.
    assert (
        "git -C /home/ubuntu/server branch -D worktree-fanout-1 >/dev/null 2>&1 && "
        'echo "removed: /w1 (already gone)"' in cmd
    )


def test_the_merged_test_asks_the_forge_because_this_repo_squash_merges():
    """Ruling 36: ancestry exits 1 for a squashed branch, so it cannot decide this."""
    cmd = remote_clean_command(B1)
    assert "merge-base" not in cmd
    assert (
        "merged=$(cd /home/ubuntu/server && gh pr list --state merged "
        "--head worktree-fanout-1 --json headRefOid --jq '.[].headRefOid' 2>/dev/null"
        in cmd
    )
    # Ruling 38: the merged PR's head SHA must equal this branch's tip. Branch names are
    # reused here, so a name-only match would delete unlanded work.
    assert (
        "tip=$(git -C /home/ubuntu/server rev-parse refs/heads/worktree-fanout-1 "
        "2>/dev/null)" in cmd
    )
    assert '| grep -c -x "${tip:-none}")' in cmd
    assert cmd.index("tip=$(git") < cmd.index("gh pr list")
    # A gh that is missing, unauthenticated or offline answers nothing, which must read as
    # zero rather than as an error that skips both echoes.
    assert "case \"${merged}\" in ''|*[!0-9]*) merged=0;; esac" in cmd
    assert cmd.index("merged=0;; esac") < cmd.index('if [ "${merged}" -gt 0 ]')


def test_the_gone_tree_branch_prunes_its_own_stale_registration():
    """Ruling 37: prune skips a locked tree, so the launch lock is released first."""
    cmd = remote_clean_command(B1)
    assert (
        "git -C /home/ubuntu/server worktree unlock /w1 2>/dev/null; "
        "git -C /home/ubuntu/server worktree prune;" in cmd
    )
    # Inside the gone-tree branch, after both branch outcomes — not on the path that still
    # has a tree to hand to `clean-one`.
    assert cmd.index("worktree prune") < cmd.index("else cd /home/ubuntu/server")
    assert cmd.index(
        'echo "kept: /w1 — branch worktree-fanout-1 unmerged, tree gone"'
    ) < (cmd.index("worktree prune"))


def test_a_second_pass_makes_no_remote_call_for_a_batch_already_removed(tmp_path):
    """The convergence F1 says is impossible: pass one removes 1, pass two removes 2."""
    manifest = _manifest(tmp_path, [B1, B2])
    first, _calls = fake_tools(
        answers={"daniel-box": ok("removed: /w1"), "daniel-server": ok(UNMERGED)}
    )
    assert _clean(first, tmp_path, manifest.run_id) == 0
    assert manifest_path(manifest.run_id, tmp_path).exists()
    written = json.loads(manifest_path(manifest.run_id, tmp_path).read_text())
    assert written["batches"][0]["removed_at"]
    assert written["batches"][1]["removed_at"] is None

    second, calls = fake_tools(answers={"daniel-server": ok("removed: /w2")})
    assert _clean(second, tmp_path, manifest.run_id) == 0
    # Batch 1 costs no ssh at all the second time: the manifest remembers it.
    assert [host for host, _cmd, _stdin in calls.calls] == ["daniel-server"]
    assert not manifest_path(manifest.run_id, tmp_path).exists()


def test_a_batch_removed_earlier_is_named_rather_than_silently_skipped(
    tmp_path, capsys
):
    cleaned = Batch(
        "1", "daniel-box", "/w1", "b1", "u1", [1], "t", "2026-09-10T12:00:00+00:00"
    )
    manifest = _manifest(tmp_path, [cleaned, B2])
    tools, _calls = fake_tools(answers={"daniel-server": ok("removed: /w2")})
    assert _clean(tools, tmp_path, manifest.run_id) == 0
    out = capsys.readouterr().out
    assert "1 on daniel-box: removed earlier (2026-09-10T12:00:00+00:00)" in out


def test_an_unreachable_host_is_a_failure_not_a_kept_tree(tmp_path, capsys):
    """F7: `kept` sends the operator to wait for a merge that already happened."""
    manifest = _manifest(tmp_path, [B2])
    refused = subprocess.CompletedProcess(
        [], 255, stdout="", stderr="ssh: connect to host daniel-server port 22: refused"
    )
    tools, _calls = fake_tools(answers={"daniel-server": refused})
    assert _clean(tools, tmp_path, manifest.run_id) == 1
    assert manifest_path(manifest.run_id, tmp_path).exists()
    out = capsys.readouterr().out
    assert "2 on daniel-server: clean failed (exit 255): ssh: connect" in out
    assert "clean failed for 2 — see above" in out
    assert "re-run clean once merged" not in out


def test_a_kept_verdict_stays_exit_zero(tmp_path):
    """The leg spoke, so its verdict stands whatever the exit status."""
    manifest = _manifest(tmp_path, [B2])
    spoke = subprocess.CompletedProcess([], 1, stdout=UNMERGED, stderr="")
    tools, _calls = fake_tools(answers={"daniel-server": spoke})
    assert _clean(tools, tmp_path, manifest.run_id) == 0
    assert manifest_path(manifest.run_id, tmp_path).exists()
