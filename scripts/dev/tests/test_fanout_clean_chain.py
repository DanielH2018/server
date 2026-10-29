"""`remote_clean_command`'s gone-tree shell chain, EXECUTED against a scratch repo.

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

import shlex
import subprocess
import shutil
import socket
import threading
from functools import partial
from unittest import mock

import pytest

from fanout_lib import clean
from fanout_lib.clean import live_process_scan, remote_clean_command
from fanout_lib.manifest import Batch
from lib.worktrees import escape_holder
from lib.git_testing import git, git_out, init_repo, scrubbed_env
from lib.proc_testing import fake_bin, path_with, run
from lib.socket_testing import listening, short_socket_path

BRANCH = "worktree-fanout-x"
UNIT = "fanout-x"


def _scrubbed_env(extra_path=None):
    """`lib.git_testing.scrubbed_env`, with `extra_path` prepended to PATH.

    The chain under test runs through a stub `bin` directory, which is the one thing the
    shared scrub does not know about.
    """
    env = scrubbed_env()
    if extra_path:
        env["PATH"] = path_with(extra_path, env=env)
    return env


def _branches(repo):
    return git(repo, "branch", "--list").stdout


def _registrations(repo):
    return git(repo, "worktree", "list", "--porcelain").stdout


def _scratch_with_a_gone_worktree(tmp_path):
    """Build the state the gone-tree branch exists for: a registration with nothing behind it.

    The `origin` is real because the chain's `git fetch --quiet origin master` is `&&`-joined
    to everything after it — without a remote it short-circuits and the test proves nothing.
    The worktree is locked the way `launch` locks it, so the chain's `worktree unlock` has
    something to release.

    Returns:
        `(repo, worktree path, the branch's tip SHA)`.
    """
    origin = init_repo(tmp_path / "origin.git", bare=True)
    repo = init_repo(tmp_path / "repo", initial_commit="init")
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "-q", "origin", "master")
    worktree = tmp_path / "w"
    git(repo, "worktree", "add", "-q", "-b", BRANCH, str(worktree))
    git(worktree, "commit", "-q", "-m", "work", "--allow-empty", "--no-gpg-sign")
    tip = git_out(repo, "rev-parse", f"refs/heads/{BRANCH}")
    git(repo, "worktree", "lock", "--reason", UNIT, str(worktree))
    shutil.rmtree(worktree)
    return repo, worktree, tip


def _stub_bin(tmp_path, gh_prints=None, unit_active=False, gh_script=None):
    """A bin directory to prepend to PATH, holding the stubs this chain must not escape.

    `systemctl` is always stubbed and records its arguments to `systemctl-calls`; the chain
    resets a transient unit, which on this host would reach the real user manager. Its
    `is-active` answers `unit_active` — 0 for active, 3 for inactive, systemctl's own codes —
    and every other verb exits 0. A stub that exited 0 for everything would read every unit
    as active and send every chain down the refusal branch.

    `gh_prints=None` models a host with no `gh` — as a stub that exits 127 printing nothing,
    which is what an absent binary looks like from inside the chain's pipeline. Simply
    leaving `gh` out of the directory would not: PATH still reaches this host's REAL `gh`,
    and leakguard catches that as a live call to the forge.
    """
    bin_dir = tmp_path / "bin"
    is_active_rc = 0 if unit_active else 3
    return fake_bin(
        bin_dir,
        systemctl=(
            f'#!/bin/sh\necho "$@" >> {bin_dir / "systemctl-calls"}\n'
            f'case "$2" in is-active) exit {is_active_rc} ;; esac\nexit 0\n'
        ),
        gh=(
            gh_script
            if gh_script is not None
            else '#!/bin/sh\necho "gh: command not found" >&2\nexit 127\n'
            if gh_prints is None
            else f"#!/bin/sh\nprintf '%s' {shlex.quote(gh_prints)}\n"
        ),
    )


def _run_chain(repo, worktree, stub_bin, proc_root=None):
    """Run the chain under bash, its in-use scan walking `proc_root`.

    `proc_root` defaults to an empty directory, because the scan forks per pid and the host's
    process count then sets the test's cost: about 4s a test on daniel-box against 1.6s on a CI
    runner (#4873, the shape #4876 fixed in test_fanout_abandon.py). Only a test that needs the
    scan to find a live process passes `/proc`.
    """
    if proc_root is None:
        proc_root = stub_bin.parent / "proc"
        proc_root.mkdir()
    batch = Batch("x", "h", str(worktree), BRANCH, UNIT, [1], "t")
    # `remote_clean_command` takes no proc-root seam, so the scan it calls is wrapped here.
    scan = partial(live_process_scan, proc_root=str(proc_root))
    with mock.patch.object(clean, "live_process_scan", scan):
        cmd = remote_clean_command(batch, repo=str(repo))
    # The guard that makes running this safe: never let it name the shared checkout.
    assert str(repo) in cmd and "/home/ubuntu/server" not in cmd
    return run(["bash", "-c", cmd], env=_scrubbed_env(extra_path=str(stub_bin)))


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


def _gh_merged_head_and_compare(merged_head, status):
    """A `gh` whose `pr list` names `merged_head` and whose `api` compare answers `status`."""
    return (
        "#!/bin/sh\n"
        f'case "$1" in api) printf "%s\\n" {shlex.quote(status)} ;; '
        f'*) printf "%s\\n" {shlex.quote(merged_head)} ;; esac\n'
    )


def test_a_branch_inside_the_merged_pr_head_is_deleted(tmp_path):
    """#6365: `land.sh` merged master into the branch, so the merged head is the tip plus a merge.

    The SHAs differ, but GitHub's compare calls the tip an ancestor of the merged head, which
    means no local commit is unlanded.
    """
    repo, worktree, _tip = _scratch_with_a_gone_worktree(tmp_path)
    gh = _gh_merged_head_and_compare("1" * 40, "ahead")
    proc = _run_chain(repo, worktree, _stub_bin(tmp_path, gh_script=gh))
    assert proc.stdout.strip() == f"removed: {worktree} (already gone)"
    assert BRANCH not in _branches(repo)


def test_a_branch_with_commits_the_merged_pr_head_lacks_survives(tmp_path):
    """The reject half of #6365: a diverged tip holds work nothing proves landed."""
    repo, worktree, _tip = _scratch_with_a_gone_worktree(tmp_path)
    gh = _gh_merged_head_and_compare("1" * 40, "diverged")
    proc = _run_chain(repo, worktree, _stub_bin(tmp_path, gh_script=gh))
    assert "unmerged, tree gone" in proc.stdout
    assert BRANCH in _branches(repo)


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
    """The scoping check, executed: a sibling stale registration must survive.

    Ruling 37's repo-global `git worktree prune` took every registration whose directory was
    missing. This asserts the narrowed `worktree remove` does not.
    """
    repo, worktree, tip = _scratch_with_a_gone_worktree(tmp_path)
    sibling = tmp_path / "sibling"
    git(repo, "worktree", "add", "-q", "-b", "worktree-other", str(sibling))
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
    assert (stub_bin / "systemctl-calls").read_text().splitlines() == [
        f"--user is-active --quiet {UNIT}",
        f"--user reset-failed {UNIT}",
    ]


def test_an_active_unit_is_kept_and_nothing_is_touched(tmp_path):
    """FLAGGED half: a batch still running is never cleaned out from under itself.

    Executed with the merged answer that would otherwise delete the branch: the refusal has
    to come before the merge check, because a clean tree at master is exactly what a running
    batch that has not committed yet looks like.
    """
    repo, worktree, tip = _scratch_with_a_gone_worktree(tmp_path)
    stub_bin = _stub_bin(tmp_path, f"{tip}\n", unit_active=True)
    proc = _run_chain(repo, worktree, stub_bin)
    assert proc.stdout.strip() == (
        f"kept: {worktree} — unit {UNIT} still active; stop it first"
    )
    assert BRANCH in _branches(repo)
    assert str(worktree) in _registrations(repo)
    # Nothing past the probe ran: no reset, no fetch, no gh.
    assert (stub_bin / "systemctl-calls").read_text().splitlines() == [
        f"--user is-active --quiet {UNIT}"
    ]


def test_an_inactive_unit_is_cleaned(tmp_path):
    """CLEAN half: `stop` (or a finished run) makes the unit inactive, and the chain proceeds."""
    repo, worktree, tip = _scratch_with_a_gone_worktree(tmp_path)
    proc = _run_chain(
        repo, worktree, _stub_bin(tmp_path, f"{tip}\n", unit_active=False)
    )
    assert proc.stdout.strip() == f"removed: {worktree} (already gone)"


def _stub_uv(stub_bin):
    """A `uv` that records its argv and speaks a `kept:` verdict, so the checkout branch is provable.

    The real interpreter leg would run `clean-one` for real; what these tests need is
    evidence the chain REACHED it, and that it did not reach it for a stub directory.
    """
    return fake_bin(
        stub_bin,
        uv=f'#!/bin/sh\necho "$@" >> {stub_bin / "uv-calls"}\necho "kept: by-uv"\n',
    )


def test_a_directory_that_is_no_longer_a_checkout_reads_as_gone(tmp_path):
    """A worktree removed on exit leaves a `.remember/` stub where the tree was.

    `-e` on that path is true, so a chain keyed on it would take the checkout branch and
    run the script inside a tree that no longer carries one — `python: can't open file`,
    exit 2, the leg `failed`, and the manifest never converging. The discriminator is the
    `.git` file every linked worktree carries, not the directory. The stub goes with the
    tree: a relaunch of the same batch id refuses on `test ! -e <worktree>`.
    """
    repo, worktree, tip = _scratch_with_a_gone_worktree(tmp_path)
    (worktree / ".remember" / "logs").mkdir(parents=True)
    stub_bin = _stub_uv(_stub_bin(tmp_path, f"{tip}\n"))
    proc = _run_chain(repo, worktree, stub_bin)
    assert proc.stdout.strip() == f"removed: {worktree} (already gone)"
    assert not worktree.exists()
    assert BRANCH not in _branches(repo)
    assert str(worktree) not in _registrations(repo)
    assert not (stub_bin / "uv-calls").exists()


def test_a_directory_that_is_still_a_checkout_reaches_clean_one(tmp_path):
    """The reject half: a live worktree is judged by `clean-one`, never by the shell chain."""
    repo, worktree, tip = _scratch_with_a_gone_worktree(tmp_path)
    git(repo, "worktree", "unlock", str(worktree))
    git(repo, "worktree", "remove", "--force", str(worktree))
    git(repo, "worktree", "add", "-q", str(worktree), BRANCH)
    stub_bin = _stub_uv(_stub_bin(tmp_path, f"{tip}\n"))
    proc = _run_chain(repo, worktree, stub_bin)
    assert proc.stdout.strip() == "kept: by-uv"
    assert (stub_bin / "uv-calls").read_text().split()[-3:] == [
        "clean-one",
        str(worktree),
        BRANCH,
    ]
    assert worktree.exists()
    assert BRANCH in _branches(repo)


def test_a_stub_a_live_process_still_uses_is_kept_not_deleted(tmp_path):
    """#3995: the gone-tree leg deleted the stub without `remove`'s live-process refusal."""
    repo, worktree, tip = _scratch_with_a_gone_worktree(tmp_path)
    (worktree / ".remember").mkdir(parents=True)
    holder = subprocess.Popen(["sleep", "60"], cwd=worktree)
    try:
        proc = _run_chain(
            repo, worktree, _stub_bin(tmp_path, f"{tip}\n"), proc_root="/proc"
        )
    finally:
        holder.kill()
        holder.wait()
    assert proc.stdout.strip() == f"kept: {worktree} — pid {holder.pid} still uses it"
    assert (worktree / ".remember").is_dir()
    assert BRANCH in _branches(repo)


@pytest.fixture
def sock():
    """A socket path under `/tmp`, which a long `tmp_path` would push past `sun_path` (#4763)."""
    with short_socket_path() as path:
        yield path


def _serving(sock, answer: bytes) -> threading.Thread:
    """A listener at `sock` that sends `answer` to one connection, as the service does."""
    listener = listening(sock)

    def one():
        with listener:
            listener.settimeout(30)
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                return
            with conn:
                conn.sendall(answer)

    thread = threading.Thread(target=one, daemon=True)
    thread.start()
    return thread


def _scan(worktree, sock) -> subprocess.CompletedProcess:
    """`live_process_scan` run under bash, asking `sock` through the real `nc -U`.

    Its `/proc` loop walks an empty directory, as in `_run_chain`: no process sits in a
    `tmp_path` tree, so the host's pids could only add cost, never change `busy`.
    """
    proc_root = worktree.parent / "proc"
    proc_root.mkdir(exist_ok=True)
    script = (
        live_process_scan(str(worktree), sock=str(sock), proc_root=str(proc_root))
        + 'echo "$busy"'
    )
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=True, timeout=30
    )


def _busy_with_helper(sock, worktree, answer: bytes) -> str:
    """What `live_process_scan` sets `busy` to when a helper at `sock` answers `answer`."""
    thread = _serving(sock, answer)
    busy = _scan(worktree, sock).stdout.strip()
    thread.join(timeout=10)
    return busy


def test_the_root_helper_sees_another_uids_holder_the_proc_loop_cannot_is_flagged(
    tmp_path, sock
):
    """#4170: a `systemd-run --uid=claude` process in the tree is invisible to /proc as ubuntu."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    sibling = f"{worktree}-other"
    answer = f"ok\n41\tcwd\t{sibling}\n42\tcwd\t{worktree}/sub\nend\n".encode()

    assert _busy_with_helper(sock, worktree, answer) == "42"


def test_a_refused_garbled_truncated_or_blind_answer_keeps_the_tree_is_flagged(
    tmp_path, sock
):
    # A socket carries no exit status, so the framing is what tells a finished answer from an
    # instance that died mid-scan.
    worktree = tmp_path / "wt"
    worktree.mkdir()

    for answer in (b"refused\tno root\n", b"", b"ok\n", b"ok\n7 cwd /x\nend\n"):
        assert _busy_with_helper(sock, worktree, answer).startswith("unknown"), answer
        sock.unlink()
    blind = b"ok\n7\tunreadable\tcwd: EPERM\nend\n"
    assert _busy_with_helper(sock, worktree, blind) == "7"


def test_a_socket_nothing_listens_on_keeps_the_tree_is_flagged(tmp_path, sock):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as bound:
        bound.bind(str(sock))
        busy = _scan(worktree, sock).stdout.strip()

    assert busy.startswith("unknown")


def test_no_socket_falls_back_to_the_proc_loop_and_says_so_is_clean(tmp_path):
    # A host the apply has not reached. A shell under claude-rc.service's NoNewPrivileges=yes
    # no longer lands here: it connects like any other (#4297).
    worktree = tmp_path / "wt"
    worktree.mkdir()

    proc = _scan(worktree, tmp_path / "absent.sock")

    assert proc.stdout.strip() == ""
    assert "cannot connect to" in proc.stderr


def test_a_socket_this_uid_may_not_write_falls_back_and_says_so_is_clean(
    tmp_path, sock
):
    # A session that predates its user's group grant: the root scan does not apply yet, so
    # the chain scans /proc itself rather than keep every tree.
    worktree = tmp_path / "wt"
    worktree.mkdir()
    with listening(sock):
        sock.chmod(0o000)
        proc = _scan(worktree, sock)

    assert proc.stdout.strip() == ""
    assert "cannot connect to" in proc.stderr


def test_a_helper_that_names_only_other_trees_leaves_the_tree_free_is_clean(
    tmp_path, sock
):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    answer = f"ok\n41\tcwd\t{worktree}-other\nend\n".encode()

    assert _busy_with_helper(sock, worktree, answer) == ""


def test_a_holder_whose_cwd_has_a_line_break_holds_only_its_tree_is_clean(
    tmp_path, sock
):
    # #4294: the helper escapes the cwd, and the awk compares escaped forms, so the holder of
    # the oddly named tree is found and an unrelated tree stays free.
    odd = tmp_path / "wt\nx y"
    odd.mkdir()
    plain = tmp_path / "wt"
    plain.mkdir()
    answer = f"ok\n42\tcwd\t{escape_holder(str(odd))}/sub\nend\n".encode()

    assert _busy_with_helper(sock, odd, answer) == "42"
    sock.unlink()
    assert _busy_with_helper(sock, plain, answer) == ""


def test_an_unescaped_line_break_in_an_answer_is_flagged(tmp_path, sock):
    # The red half: the same cwd printed as-is splits the record and the answer is garbled.
    odd = tmp_path / "wt\nx"
    odd.mkdir()
    answer = f"ok\n42\tcwd\t{odd}/sub\nend\n".encode()

    assert _busy_with_helper(sock, odd, answer).startswith("unknown")
