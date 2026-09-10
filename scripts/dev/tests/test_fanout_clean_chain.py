"""`remote_clean_command`'s gone-tree shell chain, EXECUTED against a scratch repo — #1677.

Its siblings in test_fanout_clean_convergence.py assert the chain by shape, which a quoting
regression in the `gh pr list … --json headRefOid` / `grep -c -x` pair would pass. These run
it under `bash -c` with a fake `gh` on PATH instead, so the quoting is what decides the
verdict. That is not theoretical: running the chain is what found the `branch -D`-before-
deregister ordering bug the shape assertions had passed for months.

Every test passes `remote_clean_command` its `repo` seam, pointing at a scratch checkout.
Aimed at the default instead, this chain
runs `fetch`, `branch -D` and `worktree remove` against /home/ubuntu/server — the checkout
every live session on this host shares — and the isolation guard cannot see it, because it
reads Bash tool command text and this is a pytest subprocess.

The chain's `systemctl --user reset-failed` goes to a recording stub on PATH rather than this
host's real user manager, which is what leakguard requires. `test_the_chain_resets_the_units_failed_state`
asserts the stub recorded a call, so the stub cannot silently stop being reached.

Run: uv run pytest scripts/dev/tests/test_fanout_clean_chain.py
"""

import os
import shlex
import shutil
import subprocess

from fanout_lib.clean import remote_clean_command
from fanout_lib.manifest import Batch

BRANCH = "worktree-fanout-x"
UNIT = "fanout-x"


def _scrubbed_env(extra_path=None):
    """The real environment minus every GIT_* variable, plus a scratch identity.

    `prek`'s pytest hook runs with `GIT_DIR` set, and git resolves that before `-C`, so an
    unscrubbed call reads and writes the real checkout however carefully `-C` is aimed —
    the same trap `test_fanout_clean.py`'s own `_git` documents. `GIT_CONFIG_GLOBAL` goes to
    /dev/null because this host configures SSH commit signing globally, which a scratch
    commit inheriting it cannot satisfy.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "t"
    env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "t@example.invalid"
    env["GIT_CONFIG_GLOBAL"] = env["GIT_CONFIG_SYSTEM"] = os.devnull
    if extra_path:
        env["PATH"] = f"{extra_path}{os.pathsep}{env['PATH']}"
    return env


def _git(cwd, *args, capture=False):
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=_scrubbed_env(),
        check=True,
        capture_output=capture,
        text=capture,
    )


def _branches(repo):
    return _git(repo, "branch", "--list", capture=True).stdout


def _registrations(repo):
    return _git(repo, "worktree", "list", "--porcelain", capture=True).stdout


def _scratch_with_a_gone_worktree(tmp_path):
    """Build the state the gone-tree branch exists for: a registration with nothing behind it.

    The `origin` is real because the chain's `git fetch --quiet origin master` is `&&`-joined
    to everything after it — without a remote it short-circuits and the test proves nothing.
    The worktree is locked the way `launch` locks it, so the chain's `worktree unlock` has
    something to release.

    Returns:
        `(repo, worktree path, the branch's tip SHA)`.
    """
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(origin))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "--initial-branch=master")
    _git(repo, "commit", "-q", "-m", "init", "--allow-empty", "--no-gpg-sign")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "origin", "master")
    worktree = tmp_path / "w"
    _git(repo, "worktree", "add", "-q", "-b", BRANCH, str(worktree))
    _git(worktree, "commit", "-q", "-m", "work", "--allow-empty", "--no-gpg-sign")
    tip = _git(repo, "rev-parse", f"refs/heads/{BRANCH}", capture=True).stdout.strip()
    _git(repo, "worktree", "lock", "--reason", UNIT, str(worktree))
    shutil.rmtree(worktree)
    return repo, worktree, tip


def _stub_bin(tmp_path, gh_prints=None):
    """A bin directory to prepend to PATH, holding the stubs this chain must not escape.

    `systemctl` is always stubbed and records its arguments to `systemctl-calls`; the chain
    resets a transient unit, which on this host would reach the real user manager.

    `gh_prints=None` models a host with no `gh` — as a stub that exits 127 printing nothing,
    which is what an absent binary looks like from inside the chain's pipeline. Simply
    leaving `gh` out of the directory would not: PATH still reaches this host's REAL `gh`,
    and leakguard catches that as a live call to the forge.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    systemctl = bin_dir / "systemctl"
    systemctl.write_text(f'#!/bin/sh\necho "$@" >> {bin_dir / "systemctl-calls"}\n')
    systemctl.chmod(0o755)
    gh = bin_dir / "gh"
    if gh_prints is None:
        gh.write_text('#!/bin/sh\necho "gh: command not found" >&2\nexit 127\n')
    else:
        gh.write_text(f"#!/bin/sh\nprintf '%s' {shlex.quote(gh_prints)}\n")
    gh.chmod(0o755)
    return bin_dir


def _run_chain(repo, worktree, stub_bin):
    batch = Batch("x", "h", str(worktree), BRANCH, UNIT, [1], "t")
    cmd = remote_clean_command(batch, repo=str(repo))
    # The guard that makes running this safe: never let it name the shared checkout.
    assert str(repo) in cmd and "/home/ubuntu/server" not in cmd
    return subprocess.run(
        ["bash", "-c", cmd],
        capture_output=True,
        text=True,
        env=_scrubbed_env(extra_path=str(stub_bin)),
    )


def test_a_branch_whose_merged_pr_head_matches_its_tip_is_deleted(tmp_path):
    """The accept half: gh answers this exact tip, so the branch really did land.

    This is the case the shape assertions could not see. Deleting the branch before
    deregistering the worktree made git refuse, and every merged gone-tree batch read
    `kept: … not deleted` — the state `cmd_clean` tells the operator to re-run once merged.
    """
    repo, worktree, tip = _scratch_with_a_gone_worktree(tmp_path)
    proc = _run_chain(repo, worktree, _stub_bin(tmp_path, f"{tip}\n"))
    assert proc.stdout.strip() == f"removed: {worktree} (already gone)"
    assert BRANCH not in _branches(repo)
    assert str(worktree) not in _registrations(repo)


def test_a_branch_whose_merged_pr_head_is_another_sha_survives(tmp_path):
    """The reject half: branch names are reused here, so a name match must not delete work."""
    repo, worktree, _tip = _scratch_with_a_gone_worktree(tmp_path)
    proc = _run_chain(repo, worktree, _stub_bin(tmp_path, "0" * 40 + "\n"))
    assert (
        proc.stdout.strip() == f"kept: {worktree} — branch {BRANCH} unmerged, tree gone"
    )
    assert BRANCH in _branches(repo)
    # The stale registration goes either way — only the branch was ever in question.
    assert str(worktree) not in _registrations(repo)


def test_a_gh_that_answers_nothing_leaves_the_branch_alone(tmp_path):
    """An unauthenticated or offline gh must read as not-merged, never as merged."""
    repo, worktree, _tip = _scratch_with_a_gone_worktree(tmp_path)
    proc = _run_chain(repo, worktree, _stub_bin(tmp_path, ""))
    assert "unmerged, tree gone" in proc.stdout
    assert BRANCH in _branches(repo)


def test_a_gh_that_is_not_installed_leaves_the_branch_alone(tmp_path):
    """`grep -c -x` over an empty pipe answers 0, so no gh at all is not-merged."""
    repo, worktree, _tip = _scratch_with_a_gone_worktree(tmp_path)
    proc = _run_chain(repo, worktree, _stub_bin(tmp_path))
    assert "unmerged, tree gone" in proc.stdout
    assert BRANCH in _branches(repo)


def test_the_chain_deregisters_only_this_batchs_worktree(tmp_path):
    """#1677's scoping complaint, executed: a sibling stale registration must survive.

    Ruling 37's repo-global `git worktree prune` took every registration whose directory was
    missing. This asserts the narrowed `worktree remove` does not.
    """
    repo, worktree, tip = _scratch_with_a_gone_worktree(tmp_path)
    sibling = tmp_path / "sibling"
    _git(repo, "worktree", "add", "-q", "-b", "worktree-other", str(sibling))
    shutil.rmtree(sibling)
    _run_chain(repo, worktree, _stub_bin(tmp_path, f"{tip}\n"))
    registrations = _registrations(repo)
    assert str(worktree) not in registrations
    assert str(sibling) in registrations


def test_the_chain_resets_the_units_failed_state(tmp_path):
    """Ruling 11, and the proof the systemctl stub is reached rather than failing open."""
    repo, worktree, tip = _scratch_with_a_gone_worktree(tmp_path)
    stub_bin = _stub_bin(tmp_path, f"{tip}\n")
    _run_chain(repo, worktree, stub_bin)
    assert (stub_bin / "systemctl-calls").read_text().strip() == (
        f"--user reset-failed {UNIT}"
    )
