"""The git state outside a red batch's worktree, snapshotted before the red phase (#3864, #3879).

Real git throughout: what is claimed is which writes git keeps where, and a fake would agree
with whatever the snapshot assumed.

Run: uv run pytest scripts/dev/tests/test_fanout_git_state.py
"""

import json

from _review_fakes import PR, _pipeline, _report
from lib.git_testing import commit, git, git_out, init_repo, scrub_process_git_env

from fanout_lib.git_state import changed, snapshot
from fanout_lib.processes import run_process
from fanout_lib.red_gate import Gate, Gates
from fanout_lib.worktree_reset import reset_worktree


def _worktree(tmp_path, monkeypatch):
    """A repo and a linked worktree, as a batch has, with a private global config."""
    scrub_process_git_env(monkeypatch)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    (tmp_path / "gitconfig").write_text("[user]\n\tname = t\n")
    repo = tmp_path / "repo"
    init_repo(repo)
    commit(repo, "init", **{"a.txt": "one\n"})
    tree = tmp_path / "tree"
    git(repo, "worktree", "add", "-q", "-b", "worktree-batch", str(tree))
    return repo, tree


def test_a_branch_key_another_session_writes_changes_nothing(tmp_path, monkeypatch):
    repo, tree = _worktree(tmp_path, monkeypatch)
    before = snapshot(run_process, tree)
    git(repo, "config", "branch.worktree-other.remote", "origin")
    assert changed(before, snapshot(run_process, tree)) == []
    assert "config:core.bare" in before.entries


def test_a_planted_hook_filter_include_or_fsmonitor_is_named(tmp_path, monkeypatch):
    repo, tree = _worktree(tmp_path, monkeypatch)
    before = snapshot(run_process, tree)
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    (hooks / "post-checkout").write_text("#!/bin/sh\ntouch /tmp/pwned\n")
    git(repo, "config", "filter.x.smudge", "sh -c 'id'")
    git(repo, "config", "include.path", "/tmp/evil")
    git(repo, "config", "core.fsmonitor", "/tmp/evil.sh")
    assert changed(before, snapshot(run_process, tree)) == [
        "config:core.fsmonitor",
        "config:filter.x.smudge",
        "config:include.path",
        "hooks/post-checkout",
    ]


def test_info_attributes_and_the_global_config_are_named(tmp_path, monkeypatch):
    repo, tree = _worktree(tmp_path, monkeypatch)
    before = snapshot(run_process, tree)
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "attributes").write_text("* filter=x\n")
    (tmp_path / "gitconfig").write_text("[core]\n\thooksPath = /tmp/evil\n")
    assert changed(before, snapshot(run_process, tree)) == [
        "global:core.hookspath",
        "global:user.name",
        "info/attributes",
    ]


def test_a_rewritten_git_pointer_is_named(tmp_path, monkeypatch):
    _, tree = _worktree(tmp_path, monkeypatch)
    before = snapshot(run_process, tree)
    other = tmp_path / "other"
    init_repo(other)
    (tree / ".git").write_text(f"gitdir: {other / '.git'}\n")
    names = changed(before, snapshot(run_process, tree))
    assert names[:2] == ["the worktree's git dir", ".git"]


def _red_batch(tmp_path, red_author):
    """A red batch whose red phase runs `red_author`, and the claude phases it reached."""
    reports = [
        _report(structured={"behaviours": []}),
        _report(f"Opened {PR}"),
        _report(structured={"summary": "", "findings": []}),
    ]
    gates = Gates(red=lambda *_: Gate("refused"), green=lambda *_: "")
    pipeline, run = _pipeline(tmp_path, reports, gates=gates)

    def runner(argv, stdin):
        if (
            "--json-schema" in argv
            and (tmp_path / ".fanout" / "phase").read_text() == "red\n"
        ):
            red_author()
        return run(argv, stdin)

    pipeline.run = runner
    return pipeline, run


def test_a_red_phase_that_rewrites_the_git_pointer_fails_before_the_implementer(
    tmp_path,
):
    def red_author():
        (tmp_path / ".git").write_text("gitdir: /tmp/elsewhere\n")

    pipeline, run = _red_batch(tmp_path, red_author)
    final = pipeline.run_all()
    assert (
        final["is_error"]
        and "changed git state outside the worktree: .git" in final["result"]
    )
    assert [phase for _, _, phase in run.claude] == ["red"]


def test_the_implementer_starts_from_the_brief_read_at_start(tmp_path):
    def red_author():
        (tmp_path / ".fanout" / "brief.md").write_text("planted\n")

    pipeline, _ = _red_batch(tmp_path, red_author)
    pipeline.run_all()
    assert (tmp_path / ".fanout" / "brief.md").read_text() == pipeline.brief


def test_a_planted_gitattributes_cannot_change_what_the_reset_writes(
    tmp_path, monkeypatch
):
    scrub_process_git_env(monkeypatch)
    repo = tmp_path / "repo"
    init_repo(repo)
    sha = commit(repo, "base", **{".gitattributes": "", "sub/a.txt": "one\ntwo\n"})
    (repo / "sub" / ".gitattributes").write_text("*.txt eol=crlf\n")
    (repo / ".gitattributes").write_text("*.txt eol=crlf\n")
    (repo / "sub" / "a.txt").write_text("changed\n")
    reset_worktree(run_process, repo, sha)
    assert (repo / "sub" / "a.txt").read_bytes() == b"one\ntwo\n"
    assert not (repo / "sub" / ".gitattributes").exists()
    assert git_out(repo, "status", "--porcelain") == ""


def test_a_replaced_report_file_still_gets_the_final_report(tmp_path):
    from fanout_review import write_report

    report = tmp_path / "report.json"
    report.write_text("")
    with report.open("r+") as out:
        report.unlink()
        report.write_text(json.dumps(_report(f"Opened {PR}")) + "\n")
        write_report(_report("failed: no PR"), out, report)
    assert report.read_text() == json.dumps(_report("failed: no PR")) + "\n"
