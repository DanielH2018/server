"""One way to run git from a script, with the repository chosen by ``cwd`` alone.

WHY. Five scripts each carried a private ``_git()`` by 2026-09-01, and they disagreed on
the two things that matter: whether a non-zero exit raises, and whether an inherited
``GIT_DIR`` could redirect the call at another repository. The second one is not
hypothetical -- inside a git hook ``GIT_DIR`` and ``GIT_WORK_TREE`` are both set and point
at the repo running the hook, and ``git -C`` does not override them, so ``head_sha()`` once
reported the hook's repo for every path it was handed.

Every ``GIT_*`` variable is stripped before the call, so ``cwd`` is the only thing that
decides which tree is read. This is for reads and worktree bookkeeping: a script that
commits and needs ``GIT_AUTHOR_*`` passes its own ``env`` to ``subprocess`` directly.
"""

import os
import subprocess
from pathlib import Path


def git(
    *args: str,
    cwd: str | Path | None = None,
    check: bool = True,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``git <args>`` in ``cwd`` and return the completed process.

    ``check=True`` raises ``CalledProcessError`` on a non-zero exit, the way most callers
    want a failed ``rev-parse`` to surface. Pass ``check=False`` to read ``returncode``
    yourself, as a ``merge-base --is-ancestor`` test does.
    """
    # Benign today: no caller of this module runs under an `Environment=GIT_...` (no
    # GIT_SSH_COMMAND, no signing config arrives by env for the three publish_pr.py crons).
    # But this strips ALL of it, including one a future cron unit adds on purpose -- e.g. a
    # signing key path -- and `git push` would then fail on all three crons with nothing
    # testing it, since this function is the one write path they share.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
    )


def git_stdout(*args: str, cwd: str | Path | None = None, **kwargs) -> str:
    """``git(...).stdout`` with surrounding whitespace removed."""
    return git(*args, cwd=cwd, **kwargs).stdout.strip()


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
    counted by a ``--porcelain`` check is what parks the GitOps deployer, and the ``.gitignore``
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


# Long enough that no live session's just-written objects are in range, short enough to bound the
# growth. See repair_object_store.
PRUNE_GRACE = "1.day.ago"


def gc_log_path(cwd: str | Path) -> Path:
    """``gc.log`` in the SHARED git directory, which is where automatic gc parks its refusal.

    Not ``--git-dir``: a worktree has a private one, and ``gc.log`` only ever lives in the
    common directory the worktrees hang off.
    """
    common = git_stdout(
        "rev-parse", "--path-format=absolute", "--git-common-dir", cwd=cwd, check=False
    )
    return Path(common or Path(cwd) / ".git") / "gc.log"


def repair_object_store(cwd: str | Path) -> list[str]:
    """Drop the unreachable objects worktree churn leaves behind, and clear a stale ``gc.log``.

    Returns one line per step taken, for a caller to print.

    Removing a worktree makes its branch's objects unreachable, and this repo removes worktrees
    constantly. ``git gc --auto`` is meant to absorb that, but it only runs once loose objects
    pass ``gc.auto`` (6700) or packs pass ``gc.autoPackLimit`` (50), and when it does run and
    still finds unreachable loose objects it may not delete it writes ``gc.log`` -- which makes
    the next ``git gc --auto`` print that log and exit instead of running, for ``gc.logExpiry``
    (one day). Nothing bounds the growth in between. Two sessions saw the warning 20 minutes
    apart on 2026-09-06 (#1435) and neither could act on it: a worktree-isolated session's git
    commands are refused against the primary checkout, so the only people who see the symptom are
    structurally unable to fix it.

    Measured on the primary checkout 2026-09-10: 2302 loose objects, 39 packs, and no ``gc.*``
    setting anywhere in the config -- both under the thresholds, with a ``gc.log`` from
    2026-09-06 still sitting there. So automatic gc is not switched off; it is simply not
    reached, and this is what does the work in its place.

    ``--expire=1.day.ago``, NEVER ``--expire=now``. Several sessions write into this object store
    at once, and an object a live session has written but not yet pointed a ref at is unreachable
    and brand new -- exactly what ``now`` deletes. A day outlasts any single session.

    Removing ``gc.log`` LAST and unconditionally: while it is there and fresh, automatic gc prints
    it and exits however clean the store has since become.

    Every step is best-effort. Callers run this after work that already succeeded, and a
    housekeeping failure must not turn that into a non-zero exit.
    """
    lines = []
    git("worktree", "prune", cwd=cwd, check=False)
    pruned = git("prune", f"--expire={PRUNE_GRACE}", cwd=cwd, check=False)
    if pruned.returncode == 0:
        lines.append(f"pruned unreachable objects older than {PRUNE_GRACE}")
    else:
        lines.append(f"git prune failed: {pruned.stderr.strip()}")
    log = gc_log_path(cwd)
    try:
        log.unlink()
        lines.append(f"removed {log} — automatic gc no longer prints it and exits")
    except FileNotFoundError:
        pass
    except OSError as error:
        lines.append(f"could not remove {log}: {error}")
    return lines
