"""The reset that returns a red batch's worktree to one commit.

`reset_worktree` says what the reset clears and why (#3852, #3871, #3884). Every git call
here runs through `hardened_runs.worktree_must`, which pins the settings the agent could
have planted in the worktree's repo.
"""

from pathlib import Path, PurePosixPath

# Reach the sibling package: a directly-invoked script gets only its own directory on
# sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys

_sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fanout_lib.hardened_runs import ResetFailed, Runner, worktree_must


def unhide_index(run: Runner, worktree: Path) -> list[str]:
    """Clear every skip-worktree and assume-unchanged bit in `worktree`'s index.

    Either bit hides an edit to a tracked file from `git status`, and `git reset --hard`
    leaves a skip-worktree file's contents as they are. `ls-files -v` tags a skip-worktree
    entry `S` and an assume-unchanged one in lowercase.

    Returns:
        The paths whose bits were cleared.

    Raises:
        ResetFailed: a git step exited non-zero.
    """
    listed = worktree_must(run, worktree, "ls-files", "-v", "-z")
    hidden = [
        entry[2:]
        for entry in listed.split("\0")
        if entry and (entry[0] == "S" or entry[0].islower())
    ]
    # One call per flag: given both, `update-index` exits 0 and clears only the last.
    for flag in ("--no-skip-worktree", "--no-assume-unchanged") if hidden else ():
        worktree_must(run, worktree, "update-index", flag, "--", *hidden)
    return hidden


# The `.fanout/` files `reset_worktree` keeps. systemd holds `report.json` and `stderr.log`
# open as the unit's stdout and stderr, so deleting either loses what `status` reads, and
# `fanout-stop.py` finds the worktree by `brief.md`. `red.json` is the red session's report,
# which nothing reads back. Everything else there is the pipeline's own and is rewritten
# before it is next read, or is a file the red author planted, such as a `land1.log` whose
# `VERDICT:` line `status` would report (#3871). A red batch is this repo's, so there is no
# `.fanout/server` snapshot to keep.
FANOUT_KEPT = ("brief.md", "report.json", "stderr.log", "red.json")


def neutral_attributes(run: Runner, worktree: Path, sha: str) -> list[str]:
    """Leave only `sha`'s own `.gitattributes` in `worktree` before the reset reads them.

    The checkout falls back to the working tree's `.gitattributes`, so an untracked one in a
    subdirectory, or an edit to a tracked one, changes the bytes the reset writes: a
    `working-tree-encoding` or `eol` line is enough (#3884). Each untracked one is deleted,
    ignored ones included, and each tracked one is checked out from `sha` first.

    Returns:
        The untracked `.gitattributes` paths deleted.
    """
    others = worktree_must(run, worktree, "ls-files", "-z", "--others").split("\0")
    planted = [f for f in others if f and PurePosixPath(f).name == ".gitattributes"]
    for name in planted:
        (worktree / name).unlink(missing_ok=True)
    tracked = worktree_must(
        run, worktree, "ls-tree", "-r", "-z", "--name-only", sha
    ).split("\0")
    own = [f for f in tracked if f and PurePosixPath(f).name == ".gitattributes"]
    if own:
        worktree_must(run, worktree, "checkout", sha, "--", *own)
    return planted


def reset_worktree(run: Runner, worktree: Path, sha: str) -> None:
    """Make `worktree` hold exactly `sha`'s tree, plus `FANOUT_KEPT`.

    The red author had the worktree before the red gate and the implementer, and neither gate
    nor the reviewer reads anything outside the commit range (#3852, #3871). A skip-worktree
    edit to a script a later phase runs, an untracked root `.mcp.json` or `CLAUDE.local.md`,
    and an ignored file the red author created or overwrote would each outlive the red phase.
    So the bits go first, then the tree is reset, then every untracked and ignored file is
    deleted. That includes `.venv/`: `uv run` rebuilds it from its cache, and a `.pth` planted
    in it would run in every later pytest. Every step runs under `hardened_runs.hardened`.

    Raises:
        ResetFailed: a git step exited non-zero, or the repo has an `info/attributes`.
    """
    # A `working-tree-encoding` or `eol` line there makes the reset itself write other bytes
    # than the commit holds, and no config setting outranks it. This repo has none.
    attributes = (
        worktree
        / worktree_must(
            run, worktree, "rev-parse", "--git-path", "info/attributes"
        ).strip()
    )
    if attributes.is_file() and attributes.stat().st_size:
        raise ResetFailed(f"{attributes} would rewrite what the reset checks out")
    unhide_index(run, worktree)
    neutral_attributes(run, worktree, sha)
    worktree_must(run, worktree, "reset", "--quiet", "--hard", sha)
    kept = [arg for name in FANOUT_KEPT for arg in ("-e", f"/.fanout/{name}")]
    # `-x` still honours `-e`, and keeps an excluded file inside a directory it otherwise
    # removes. A second `-f` removes a nested repository too.
    worktree_must(run, worktree, "clean", "-ffdxq", *kept)
