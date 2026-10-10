"""The git and pytest calls the red/green measures make, so nothing the agent wrote steers them.

Two kinds, by what the call reads. A call on the batch's worktree goes through `worktree_git`,
under the `hardened` prefix: the agent can write that repo's config, hooks, attributes and
replace refs, so each setting a call consults is pinned (#3871). `worktree_must` is the same
call for a step that may not fail, and raises `ResetFailed`.

A call that needs a tree nobody wrote goes to a fresh clone instead. `clone_at` checks a commit
out with `BARE_GIT`, which reads no global, system or worktree configuration, and `bare_git`,
`collect` and `pytest_argv` run there (#3837). `outcomes` reads the `-rA` summary of such a run.

The red gate, the green gate, the worktree reset and the two red/green measures
(`base_check`, `hunk_check`) all import from here.
"""

import re
import subprocess
from collections.abc import Callable
from pathlib import Path

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


def hardened(run: Runner, worktree: Path) -> list[str]:
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


def worktree_git(
    run: Runner, worktree: Path, *args: str
) -> subprocess.CompletedProcess:
    return run([*hardened(run, worktree), "-C", str(worktree), *args], None)


class ResetFailed(RuntimeError):
    """A git step that clears what the red phase left exited non-zero.

    The worktree may then still hold a hidden edit or the refused red commit, so the batch
    stops rather than hand it to the implementer. A leftover `index.lock` is one cause: every
    index write refuses while it exists, and `git clean` still exits 0.
    """


def worktree_must(run: Runner, worktree: Path, *args: str) -> str:
    proc = worktree_git(run, worktree, *args)
    if proc.returncode:
        raise ResetFailed(
            f"`git {' '.join(args[:2])}` exited {proc.returncode}: {proc.stderr.strip()}"
        )
    return proc.stdout


# One line of pytest's `-rA` short summary. SKIPPED lines carry a location, not a node id, so
# a skipped node is simply absent and reads as not failed.
_OUTCOME = re.compile(r"^(PASSED|FAILED|ERROR|XFAIL|XPASS) (.+?)(?: - .*)?$", re.M)


def outcomes(output: str) -> dict[str, str]:
    """Each node id in a `pytest -rA` run's short summary, with its outcome."""
    return {m.group(2): m.group(1) for m in _OUTCOME.finditer(output)}


def pytest_argv(worktree: Path, *args: str) -> list[str]:
    # `-n0` overrides the suite's `-n auto`: a handful of nodes gains nothing from workers.
    return [
        "uv", "run", "--directory", str(worktree), "pytest",
        "-p", "no:cacheprovider", "-n0", "-c", "pyproject.toml", *args,
    ]  # fmt: skip


# git with no configuration the implementer can write: no global or system file, which a
# clone's own fresh config leaves as the only other sources, and no user attributes file. The
# green gate checks HEAD out this way, so no filter, `info/attributes` line or config value
# from the worktree's repo can rewrite a file on its way to pytest (#3837).
BARE_GIT = (
    "env", "GIT_CONFIG_GLOBAL=/dev/null", "GIT_CONFIG_NOSYSTEM=1",
    "git", "-c", "core.attributesFile=/dev/null",
)  # fmt: skip


def bare_git(
    run: Runner, tree: Path, *args: str, stdin: str | None = None
) -> subprocess.CompletedProcess:
    """`git -C tree ...` under `BARE_GIT`, for a clone `clone_at` made."""
    return run([*BARE_GIT, "-C", str(tree), *args], stdin)


ORIGIN_MASTER = "refs/remotes/origin/master"


def _origin_refs(run: Runner, clone: Path, origin: str) -> str:
    """The `update-ref --stdin` input that leaves `clone` only the red gate's `origin/master`.

    A clone of a path maps the source's local branches to `origin/*`, so its `origin/master`
    is the source's local `master`. Tests that diff against `origin/master` must see the base
    the red gate saw, not that branch and not the source's live remote-tracking ref, which a
    fetch or the implementer can move between the gates (#3845). One transaction may not name
    a ref twice, so `origin/master` is set rather than deleted and recreated.
    """
    mapped = run(
        [
            *BARE_GIT, "-C", str(clone), "for-each-ref", "--format=%(refname)",
            "refs/remotes/origin/",
        ],
        None,
    ).stdout.split()  # fmt: skip
    lines = [f"delete {ref}" for ref in mapped if not (origin and ref == ORIGIN_MASTER)]
    if origin:
        lines.append(f"update {ORIGIN_MASTER} {origin}")
    return "".join(f"{line}\n" for line in lines)


def collect(run: Runner, worktree: Path, files: list[str]) -> tuple[set[str], int]:
    """The node ids pytest collects from `files`, and its exit code."""
    if not files:
        return set(), 5
    proc = run(pytest_argv(worktree, "--collect-only", "-q", *files), None)
    return {
        ln.strip() for ln in proc.stdout.splitlines() if "::" in ln
    }, proc.returncode


def clone_at(run: Runner, worktree: Path, rev: str, origin: str, tree: Path) -> str:
    """Check `rev` out into a fresh clone at `tree`; git's error, or "" on success.

    The clone reads none of the worktree's config, attributes or index, so nothing the
    implementer left there rewrites what pytest runs (#3837). Its `origin/master` is
    `origin`, or absent when that is "" (#3845).
    """
    cloned = run(
        [
            *BARE_GIT, "clone", "--quiet", "--shared", "--no-checkout",
            "--template=", str(worktree), str(tree),
        ],
        None,
    )  # fmt: skip
    if cloned.returncode == 0:
        cloned = run(
            [*BARE_GIT, "-C", str(tree), "update-ref", "--no-deref", "--stdin"],
            _origin_refs(run, tree, origin),
        )
    if cloned.returncode == 0:
        cloned = run(
            [*BARE_GIT, "-C", str(tree), "checkout", "--quiet", "--detach", rev],
            None,
        )
    if cloned.returncode:
        return cloned.stderr.strip() or f"git exited {cloned.returncode}"
    return ""
