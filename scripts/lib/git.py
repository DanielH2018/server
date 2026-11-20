"""One way to run git from a script, with the repository chosen by ``cwd`` alone.

WHY. Private ``_git()`` copies disagree on the two things that matter: whether a non-zero
exit raises, and whether an inherited ``GIT_DIR`` can redirect the call at another
repository. The second one is not hypothetical -- inside a git hook ``GIT_DIR`` and
``GIT_WORK_TREE`` are both set and point at the repo running the hook, and ``git -C`` does
not override them, so ``head_sha()`` would report the hook's repo for every path it was
handed.

Every ``GIT_*`` variable is stripped before the call, so ``cwd`` is the only thing that
decides which tree is read. This is for reads and worktree bookkeeping: a script that
commits and needs ``GIT_AUTHOR_*`` passes its own ``env`` to ``subprocess`` directly.

``git()`` runs one command to completion. ``git_process()`` starts a long-lived one, such as
``git cat-file --batch``, that a caller feeds over its pipes. Both take their environment from
``git_env()``, so the stripping has one definition.
"""

import os
import subprocess
import tarfile
import tempfile
from collections.abc import Iterable
from pathlib import Path


def git_env() -> dict[str, str]:
    """The process environment minus every ``GIT_*`` variable, for any git this module starts."""
    # Benign today: no caller of this module runs under an `Environment=GIT_...` (no
    # GIT_SSH_COMMAND, no signing config arrives by env for the three publish_pr.py crons).
    # But this strips ALL of it, including one a future cron unit adds on purpose -- e.g. a
    # signing key path -- and `git push` would then fail on all three crons with nothing
    # testing it, since `git()` is the one write path they share.
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def git(
    *args: str,
    cwd: str | Path | None = None,
    check: bool = True,
    timeout: float | None = None,
    input: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``git <args>`` in ``cwd`` and return the completed process.

    ``input`` is written to git's stdin, for a ``--stdin``/``--batch`` command.

    ``check=True`` raises ``CalledProcessError`` on a non-zero exit, the way most callers
    want a failed ``rev-parse`` to surface. Pass ``check=False`` to read ``returncode``
    yourself, as a ``merge-base --is-ancestor`` test does.
    """
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=git_env(),
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
        input=input,
    )


def git_process(*args: str, cwd: str | Path) -> subprocess.Popen[bytes]:
    """Start ``git <args>`` in ``cwd`` with binary pipes on stdin and stdout, and return it.

    For a batch command that answers one request per line, such as ``cat-file --batch``. The
    caller owns the process: close its stdin and ``wait()`` for it, or git outlives the
    caller's use of it. Stderr is inherited, so an error git prints reaches the caller's own
    stderr.
    """
    return subprocess.Popen(
        ["git", *args],
        cwd=cwd,
        env=git_env(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )


def git_stdout(*args: str, cwd: str | Path | None = None, **kwargs) -> str:
    """``git(...).stdout`` with surrounding whitespace removed."""
    return git(*args, cwd=cwd, **kwargs).stdout.strip()


def extract(
    sha: str, dest: Path, paths: Iterable[str] = (), *, repo: str | Path
) -> None:
    """Unpack `git archive` of `sha` in `repo`, narrowed to `paths` when given, under `dest`.

    A path `sha` does not hold is skipped, so a caller can name `hosts` for a commit before it.
    The archive reads the object store, so `repo`'s working tree may sit at any commit.
    """
    dest.mkdir(parents=True, exist_ok=True)
    if paths := list(paths):
        held = git_stdout("ls-tree", "--name-only", sha, "--", *paths, cwd=repo).split()
        if not (paths := [p for p in paths if p in held]):
            return
    with tempfile.TemporaryDirectory(prefix="git-extract-tar-") as tmp:
        tar_path = Path(tmp) / "tree.tar"
        git("archive", "--format=tar", "-o", str(tar_path), sha, "--", *paths, cwd=repo)
        with tarfile.open(tar_path) as tar:
            tar.extractall(dest, filter="data")


def git_dirty(
    cwd: str | Path,
    *,
    paths: tuple[str, ...] | list[str] | None = None,
    include_untracked: bool = True,
) -> bool:
    """Whether the tree at ``cwd`` has uncommitted changes.

    THE SCOPE IS THE WHOLE POINT, so it is named rather than defaulted into. "Is the tree
    dirty" reads like one question with one obvious implementation, and this repo answers it
    ten different ways across four Python callers and six shell sites -- six counting untracked
    files, three scoped to a tracked list, one scoped to two paths. Each variant is right where
    it sits; the hazard is the next caller copying whichever it meets first. An untracked file
    counted by a ``--porcelain`` check is what parks `homelab-checkout-sync`, and the ``.gitignore``
    fix for that deadlocks.

    Args:
      cwd: the tree to inspect. ``cwd`` alone decides which repository is read -- ``git()``
        strips every ``GIT_*`` variable, so an ambient ``GIT_DIR`` cannot redirect this the way
        it can redirect a bare ``git -C``.
      paths: limit the check to these pathspecs. None checks the whole tree.
      include_untracked: count untracked files as dirty. Pass False for the
        "has anything I track changed" question, which is what a job that commits a known set
        of files wants -- an operator's unrelated scratch file must not make it skip its run.

    Returns:
      True when the selected scope has any change.
    """
    args = ["status", "--porcelain"]
    if not include_untracked:
        args.append("--untracked-files=no")
    if paths:
        args += ["--", *paths]
    return bool(git_stdout(*args, cwd=cwd))
