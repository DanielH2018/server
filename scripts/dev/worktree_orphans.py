"""Judge the directories under .claude/worktrees/ that git no longer registers.

An orphan holds nothing git lacks when it has no regular files, or when a ref reaches a blob
holding each file's content (#4425). `prune_worktrees.py --prune` removes those and
keeps the rest, naming each file git never stored. The CLI there is the only caller.
"""

import shutil
import stat
import sys
from pathlib import Path

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dev.foreign_owned import foreign_owned_advice
from lib.git import git
from lib.worktrees import processes_using

ORPHAN = "orphan"


def find_orphan_dirs(worktrees_dir: str, registered: set[str]) -> list[str]:
    """Directories under .claude/worktrees/ that git no longer tracks as worktrees.

    `git worktree prune` deregisters a tree whose administrative data is gone but leaves
    the directory behind, so these are invisible to `git worktree list` and to
    `lib.worktrees.remove`. They are judged by their content instead; see unstored_files.
    """
    try:
        entries = sorted(Path(worktrees_dir).iterdir())
    except OSError:
        return []
    return [str(e) for e in entries if e.is_dir() and str(e) not in registered]


# How many unstored files a kept orphan's report names before it counts the rest. An orphaned
# full checkout carries a virtualenv of thousands.
UNSTORED_LISTED = 20


def _git_batch(repo: str, args: list[str], lines: list[str]) -> list[str] | None:
    """Feed `lines` to one git command's stdin, one per line; None unless one line comes back each."""
    result = git(
        *args, cwd=repo, check=False, input="".join(f"{line}\n" for line in lines)
    )
    out = result.stdout.splitlines()
    return out if result.returncode == 0 and len(out) == len(lines) else None


def unstored_files(repo: str, path: str) -> list[str] | None:
    """Each regular file under `path` whose content git's object store lacks, relative to it.

    An empty list means the orphan holds nothing git lacks, so removing it loses nothing:
    it has no regular files, or every one's content is a blob some ref reaches, under any
    path (#4425). None means a walk or a git call failed, so nothing is known about it.

    `git hash-object --no-filters --stdin-paths` hashes the content without `-w`, so nothing
    is written to the store. Symlinks and other non-regular entries are skipped, because
    removing one loses no file content.

    # DECIDED: "stored" means reachable from a ref (`git rev-list --objects --all`), not
    # present in the store, the check #4425 proposed. A blob staged in a worktree and never
    # committed is present but unreachable, and `repair_object_store` at the same `--prune`'s
    # tail prunes unreachable objects after a day; the orphan would have been its last copy.
    # The walk costs 0.12s for 89k objects on daniel-box.

    # DECIDED: a gitignored file counts like any other, so a virtualenv or a cache keeps its
    # orphan. Exempting ignored paths would also delete an untracked credentials file or a
    # local note, while a kept orphan costs only a hand removal: the KEEP direction.
    """
    failed = []
    files = []
    for top, _, names in Path(path).walk(on_error=failed.append):
        for name in names:
            try:
                if stat.S_ISREG((top / name).lstat().st_mode):
                    files.append(top / name)
            except OSError as exc:
                failed.append(exc)
    if failed:
        return None
    # --stdin-paths reads one path per line, so a name holding a newline cannot be hashed. It
    # counts as unstored rather than as stored.
    unstored = [f for f in files if "\n" in str(f)]
    hashable = [str(f) for f in files if "\n" not in str(f)]
    if hashable:
        shas = _git_batch(
            repo, ["hash-object", "--no-filters", "--stdin-paths"], hashable
        )
        listed = git("rev-list", "--objects", "--all", cwd=repo, check=False)
        if not shas or listed.returncode != 0:
            return None
        reachable = {line.split(" ", 1)[0] for line in listed.stdout.splitlines()}
        unstored += [
            Path(f)
            for f, sha in zip(hashable, shas, strict=True)
            if sha not in reachable
        ]
    return sorted(str(f.relative_to(path)) for f in unstored)


def orphan_report(path: str, unstored: list[str] | None) -> list[str]:
    """The long report's lines for one orphan, by what unstored_files found in it."""
    head = f"[{ORPHAN:9}] {path}"
    if unstored == []:
        return [head, "            holds nothing git lacks — `--prune` removes it"]
    if unstored is None:
        # A removal that failed on a foreign-owned path leaves exactly this: git unregisters
        # the tree after the delete fails, so the remnant surfaces here on the next run.
        return [
            head,
            "            could not read every file — check it, then remove by hand",
            *foreign_owned_advice(path),
        ]
    lines = [
        head,
        f"            {len(unstored)} file(s) hold content git never stored — check "
        "them, then remove by hand:",
    ]
    lines += [f"              {name}" for name in unstored[:UNSTORED_LISTED]]
    if len(unstored) > UNSTORED_LISTED:
        lines.append(f"              and {len(unstored) - UNSTORED_LISTED} more")
    return lines


def prune_orphans(paths: list[str]) -> int:
    """Delete each orphan directory unless a live process still uses it, one line per outcome.

    The caller passes only orphans unstored_files found empty. The in-use check is the one
    `lib.worktrees.remove` applies to a registered tree, run just before the delete.

    Returns:
      How many of `paths` were left in place, each named on stderr. The caller exits non-zero
      on any, so the weekly cron cannot read a failed removal as a clean sweep.
    """
    failed = 0
    for path in paths:
        users = processes_using(path)
        if users:
            named = "; ".join(f"pid {pid} ({how})" for pid, how in users)
            print(
                f"could not remove {path}: in use by a live process: {named}",
                file=sys.stderr,
            )
            failed += 1
            continue
        try:
            shutil.rmtree(path)
        except OSError as exc:
            print(f"could not remove {path}: {exc}", file=sys.stderr)
            for line in foreign_owned_advice(path):
                print(line, file=sys.stderr)
            failed += 1
        else:
            print(f"removed {path}")
    return failed
