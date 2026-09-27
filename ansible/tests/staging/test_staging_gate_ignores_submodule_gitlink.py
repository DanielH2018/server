"""The staging gate's dirty check must not read a stale submodule gitlink as dirty work.

`scripts/deploy_tools/staging_gate_remote.sh` refuses to render a dirty tree, because
`deploy.sh` renders from the working directory and an uncommitted edit would make the verdict
describe something other than the SHA under test. That refusal is correct for the
superproject's own files and wrong for the `Email-to-RSS` gitlink, because `git merge --ff-only`
moves the gitlink in the index and never touches the submodule's working tree. The first commit
that bumps the submodule therefore leaves ` M Email-to-RSS` in the gate's checkout, and since
the dirty check runs BEFORE the fetch, nothing the gate does afterwards can ever clear it.

That happened: e5f2ef83 bumped the gitlink on 2026-09-21, the tick of 2026-09-22 fast-forwarded
across it, and /home/ubuntu/server-staging's HEAD reflog stops there — five days of every run
answering PREP_FAILED, which the deployer maps to NO_VERDICT and deploys prod through (#2777).

Two halves, and the second is what makes this more than a spelling test. The first pins the flag
in both places that check this tree. The second runs the extracted command against a scratch
repo holding the exact state — index gitlink != the nested repo's HEAD — and proves it reports
clean there while still reporting a real uncommitted edit.

`--ignore-submodules=dirty` would pass a naive spelling check and fix nothing: it suppresses
changes INSIDE the submodule and still prints the gitlink difference. The scratch-repo half
fails on it, which is the point of running git rather than reading the script.
"""

import re
import subprocess

import pytest

from lib import yaml_fast
from _helpers import REPO

_REMOTE = REPO / "scripts" / "deploy_tools" / "staging_gate_remote.sh"
_TEARDOWN = (
    REPO / "ansible" / "roles" / "setup" / "hypervisor" / "tasks" / "teardown.yml"
)

_FLAG = "--ignore-submodules=all"


def gate_status_command() -> list[str]:
    """The `git status` invocation the gate script's dirty check actually runs.

    Read out of the script rather than hard-coded, so the scratch-repo half below exercises
    the command that ships instead of one this test invented.
    """
    match = re.search(
        r"\$\((git status --porcelain[^)]*)\)", _REMOTE.read_text(), re.MULTILINE
    )
    assert match, (
        f"{_REMOTE.name} no longer runs a `$(git status --porcelain ...)` dirty check — "
        f"this guard reads that command out of the script and now guards nothing."
    )
    return match.group(1).split()


def test_the_gate_script_ignores_submodules_in_its_dirty_check():
    assert _FLAG in gate_status_command(), (
        f"{_REMOTE.name}'s dirty check must pass {_FLAG}, or a submodule bump parks the gate "
        f"at PREP_FAILED forever (#2777)."
    )


def test_the_teardown_refusal_ignores_submodules_too():
    tasks = yaml_fast.safe_load(_TEARDOWN.read_text())
    reads = [
        task
        for task in tasks
        if "git status --porcelain" in str(task.get("ansible.builtin.command", ""))
    ]
    assert len(reads) == 1, (
        f"expected exactly one `git status --porcelain` read in {_TEARDOWN.name}, found "
        f"{len(reads)} — the refusal this pins may have moved."
    )
    assert _FLAG in reads[0]["ansible.builtin.command"]["cmd"], (
        f"{_TEARDOWN.name} refuses to remove the gate's checkout while it reads dirty. Without "
        f"{_FLAG} a stale gitlink nobody wrote blocks teardown on a tree holding no work."
    )


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo_with_a_stale_gitlink(tmp_path):
    """A superproject whose index records a submodule commit the nested repo is not on.

    Built with `update-index --cacheinfo` rather than `submodule add`, which needs a transport
    git refuses for a local path by default. The result is byte-identical in `git status`:
    a single ` M <path>` line.
    """
    sub = tmp_path / "Email-to-RSS"
    sub.mkdir()
    _git(tmp_path, "init", "-q", ".")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    _git(tmp_path, "config", "user.name", "test")
    _git(sub, "init", "-q", ".")
    _git(sub, "config", "user.email", "test@example.invalid")
    _git(sub, "config", "user.name", "test")
    (sub / "worker.js").write_text("first\n")
    _git(sub, "add", "worker.js")
    _git(sub, "commit", "-qm", "first")
    old = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=sub,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    (sub / "worker.js").write_text("second\n")
    _git(sub, "commit", "-qam", "second")

    (tmp_path / ".gitmodules").write_text(
        '[submodule "Email-to-RSS"]\n\tpath = Email-to-RSS\n\turl = ./Email-to-RSS\n'
    )
    (tmp_path / "ansible").mkdir()
    (tmp_path / "ansible" / "deploy.yml").write_text("---\n")
    _git(tmp_path, "add", ".gitmodules", "ansible/deploy.yml")
    _git(tmp_path, "update-index", "--add", "--cacheinfo", f"160000,{old},Email-to-RSS")
    _git(tmp_path, "commit", "-qm", "init")
    return tmp_path


def _status(cwd, command):
    return subprocess.run(
        command, cwd=cwd, capture_output=True, text=True, check=True
    ).stdout


def test_a_stale_gitlink_alone_reads_clean(repo_with_a_stale_gitlink):
    plain = _status(repo_with_a_stale_gitlink, ["git", "status", "--porcelain"])
    assert "Email-to-RSS" in plain, (
        "the fixture no longer reproduces a stale gitlink, so the assertion below passes "
        "vacuously"
    )
    assert _status(repo_with_a_stale_gitlink, gate_status_command()) == "", (
        "the gate's dirty check still reports a stale submodule gitlink, which is the state "
        "`git merge --ff-only` leaves behind on every submodule bump"
    )


def test_an_uncommitted_edit_to_the_tree_still_reads_dirty(repo_with_a_stale_gitlink):
    (repo_with_a_stale_gitlink / "ansible" / "deploy.yml").write_text("--- edited\n")
    assert "ansible/deploy.yml" in _status(
        repo_with_a_stale_gitlink, gate_status_command()
    ), (
        "the gate stopped seeing a real uncommitted edit — ignoring submodules must not "
        "widen into ignoring the tree deploy.sh renders from"
    )
