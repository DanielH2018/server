"""The reset that returns a red batch's worktree to one commit, and the git it runs under.

Split from `red_gate.py` at that module's length cap; `red_gate` re-exports every name here.
`reset_worktree` says what the reset clears and why (#3852, #3871, #3884).
"""

import subprocess
from collections.abc import Callable
from pathlib import Path, PurePosixPath

# One process boundary for git: argv and stdin in, the finished process out.
Runner = Callable[[list[str], str | None], subprocess.CompletedProcess]


# Settings in the worktree's own git config that would run a command, move where a reset
# writes, or keep an edit through `reset --hard`, each pinned to git's Linux default for
# every call below. `post-index-change` fires on any index write and `reference-transaction`
# on a HEAD update; a sparse checkout re-sets the skip-worktree bits `unhide_index` clears;
# an edit of the same size with its mtime restored is stat-clean unless ctime counts; with
# `fileMode` off a cleared exec bit is no change; `autocrlf` and `symlinks` change the bytes
# or the kind of file a checkout writes.
_PINNED = (
    ("core.hooksPath", "/dev/null"),
    ("core.fsmonitor", "false"),
    ("core.sparseCheckout", "false"),
    ("core.attributesFile", "/dev/null"),
    ("core.trustctime", "true"),
    ("core.checkStat", "default"),
    ("core.fileMode", "true"),
    ("core.autocrlf", "false"),
    ("core.symlinks", "true"),
    ("core.untrackedCache", "false"),
    # `ignoreCase` lets `clean` keep a planted case variant, `recurse` reaches into a
    # submodule's own config, and a split index is a second file the reset reads (#3884).
    ("core.ignoreCase", "false"),
    ("submodule.recurse", "false"),
    ("core.splitIndex", "false"),
)
# Blanking all three disables a driver; `process` wins over `smudge` when both are set.
_FILTER_KEYS = (("smudge", ""), ("clean", ""), ("process", ""), ("required", "false"))
_NO_USER_CONFIG = ("GIT_CONFIG_GLOBAL=/dev/null", "GIT_CONFIG_NOSYSTEM=1")


def _hardened(run: Runner, worktree: Path) -> list[str]:
    """The `env ... git` prefix every git call on the worktree runs under (#3871).

    The red author, and later the implementer, can write the repo's config, its hooks, its
    `info/attributes` and its replace refs, and the reset that clears the red phase would
    otherwise run through all of them. No git switch skips the repository's config file, but
    `GIT_CONFIG_COUNT` settings outrank every file, so each setting these commands consult
    is pinned, and every filter driver the config names is blanked. `GIT_WORK_TREE`
    overrides `core.worktree`. `GIT_NO_REPLACE_OBJECTS` stops a replace ref swapping the
    commit being reset to. No setting turns `info/attributes` off, so `reset_worktree`
    refuses a repo that has one.

    DECIDED: pin settings rather than snapshot and restore the config file. Every worktree
    shares the one in the common git dir and writes `branch.*` keys into it, so a restore
    would undo other sessions' writes. The reset only stops running through a planted
    `.git/config` or hook; `fanout_lib.git_state` is what fails the batch when one appears.
    """
    listed = run(
        [
            "env", *_NO_USER_CONFIG, "git", "-C", str(worktree),
            "config", "--null", "--name-only", "--get-regexp", r"^filter\.",
        ],
        None,
    ).stdout  # fmt: skip
    drivers = sorted({name.rpartition(".")[0] for name in listed.split("\0") if name})
    pinned = [
        *_PINNED,
        *(
            (f"{driver}.{key}", value)
            for driver in drivers
            for (key, value) in _FILTER_KEYS
        ),
    ]
    env = [f"GIT_CONFIG_COUNT={len(pinned)}"]
    for i, (key, value) in enumerate(pinned):
        env += [f"GIT_CONFIG_KEY_{i}={key}", f"GIT_CONFIG_VALUE_{i}={value}"]
    return [
        "env", *_NO_USER_CONFIG, "GIT_NO_REPLACE_OBJECTS=1",
        f"GIT_WORK_TREE={Path(worktree).absolute()}", *env, "git",
    ]  # fmt: skip


def _git(run: Runner, worktree: Path, *args: str) -> subprocess.CompletedProcess:
    return run([*_hardened(run, worktree), "-C", str(worktree), *args], None)


class ResetFailed(RuntimeError):
    """A git step that clears what the red phase left exited non-zero.

    The worktree may then still hold a hidden edit or the refused red commit, so the batch
    stops rather than hand it to the implementer. A leftover `index.lock` is one cause: every
    index write refuses while it exists, and `git clean` still exits 0.
    """


def _must(run: Runner, worktree: Path, *args: str) -> str:
    proc = _git(run, worktree, *args)
    if proc.returncode:
        raise ResetFailed(
            f"`git {' '.join(args[:2])}` exited {proc.returncode}: {proc.stderr.strip()}"
        )
    return proc.stdout


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
    listed = _must(run, worktree, "ls-files", "-v", "-z")
    hidden = [
        entry[2:]
        for entry in listed.split("\0")
        if entry and (entry[0] == "S" or entry[0].islower())
    ]
    # One call per flag: given both, `update-index` exits 0 and clears only the last.
    for flag in ("--no-skip-worktree", "--no-assume-unchanged") if hidden else ():
        _must(run, worktree, "update-index", flag, "--", *hidden)
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
    others = _must(run, worktree, "ls-files", "-z", "--others").split("\0")
    planted = [f for f in others if f and PurePosixPath(f).name == ".gitattributes"]
    for name in planted:
        (worktree / name).unlink(missing_ok=True)
    tracked = _must(run, worktree, "ls-tree", "-r", "-z", "--name-only", sha).split(
        "\0"
    )
    own = [f for f in tracked if f and PurePosixPath(f).name == ".gitattributes"]
    if own:
        _must(run, worktree, "checkout", sha, "--", *own)
    return planted


def reset_worktree(run: Runner, worktree: Path, sha: str) -> None:
    """Make `worktree` hold exactly `sha`'s tree, plus `FANOUT_KEPT`.

    The red author had the worktree before the red gate and the implementer, and neither gate
    nor the reviewer reads anything outside the commit range (#3852, #3871). A skip-worktree
    edit to a script a later phase runs, an untracked root `.mcp.json` or `CLAUDE.local.md`,
    and an ignored file the red author created or overwrote would each outlive the red phase.
    So the bits go first, then the tree is reset, then every untracked and ignored file is
    deleted. That includes `.venv/`: `uv run` rebuilds it from its cache, and a `.pth` planted
    in it would run in every later pytest. Every step runs under `_hardened`.

    Raises:
        ResetFailed: a git step exited non-zero, or the repo has an `info/attributes`.
    """
    # A `working-tree-encoding` or `eol` line there makes the reset itself write other bytes
    # than the commit holds, and no config setting outranks it. This repo has none.
    attributes = (
        worktree
        / _must(run, worktree, "rev-parse", "--git-path", "info/attributes").strip()
    )
    if attributes.is_file() and attributes.stat().st_size:
        raise ResetFailed(f"{attributes} would rewrite what the reset checks out")
    unhide_index(run, worktree)
    neutral_attributes(run, worktree, sha)
    _must(run, worktree, "reset", "--quiet", "--hard", sha)
    kept = [arg for name in FANOUT_KEPT for arg in ("-e", f"/.fanout/{name}")]
    # `-x` still honours `-e`, and keeps an excluded file inside a directory it otherwise
    # removes. A second `-f` removes a nested repository too.
    _must(run, worktree, "clean", "-ffdxq", *kept)
