#!/usr/bin/env python3
# gen-hooks: library
#   reason: imported by block-footguns.py for its ninth rule
"""The `block-footguns` rule that keeps a post-merge probe gate off a stale worktree.

`probe.py health <tag>` reads every repo-derived input from the checkout its own file sits
in: the role's rendered manifests, the k8s host_vars `owner`, and the alert-gate tables. An
orchestrator worktree locked hours behind master ran its post-merge gates from that tree, so
a role merged after the lock was graded against the old definition or not found at all
(#5909, #5912). `health_checkout.stale_checkout_reason` refuses two of those inputs inside the
probe; this rule refuses the run itself, before any input is read (#6110).

The rule denies `probe.py health` and `probe.py takeover` when the probe's checkout is a linked worktree and HEAD
differs from `origin/master` in a file that `origin/master` changed since the merge base,
under `ansible/` or `scripts/`. Three cases stay clean:

- The primary checkout, the remedy #5912 names. The GitOps tick fast-forwards it, so it lags
  `origin/master` by at most one tick, and inside that window `stale_checkout_reason` still
  refuses a stale role or owner.
- A worktree whose branch already holds master's change. After a squash merge the branch
  lacks the squash commit but carries the same files, so a commit count alone would refuse it.
- Any failure to read git. The rule only adds a refusal, the way the probe's own check does.

`land.sh` runs the probe through `subprocess`, so its gate never reaches this hook. It gates
from the primary or from a snapshot of the commit it deployed (`health_verdict._gated_tree`).

It lives beside `block-footguns.py` rather than in it to keep that file under the module
length cap.
"""

import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

from _hook_common import primary_checkout

# The probe subcommands that grade the cluster against a render of this tree. `health` is the
# post-merge gate `land.sh` and the deploy notifier also run; `takeover` is the D5 flip gate,
# which renders a tag to compare with what Flux would strip.
GATE_SUBCOMMANDS = frozenset({"health", "takeover"})
# The directories whose files the probe reads to form a verdict: role templates, inventory and
# host_vars under ansible/, and the probe and its tables under scripts/.
_READ_PATHS = ("ansible", "scripts")
# How many stale files the denial lists by name.
_SHOWN = 3


def _git(tree: Path, *args: str) -> subprocess.CompletedProcess | None:
    """`git -C tree <args>` with the caller's `GIT_*` stripped, or None when git fails to run."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        return subprocess.run(
            ["git", "-C", str(tree), *args],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return None


def _gate_run(words: Sequence[str], cwd: Path) -> tuple[Path, Sequence[str]] | None:
    """The probe's checkout and its arguments, when this stage is a gate run; else None.

    The probe derives its repo from its own file, so the tree is the script's, resolved against
    `cwd`, not `cwd` itself. A stage counts only when the script is the command or follows an
    interpreter, so a `grep probe.py health` stays clean.
    """
    names = [word.rsplit("/", 1)[-1] for word in words]
    if "probe.py" not in names:
        return None
    at = names.index("probe.py")
    if at and names[0] not in ("uv", "python", "python3"):
        return None
    subcommand = next((w for w in words[at + 1 :] if not w.startswith("-")), None)
    if subcommand not in GATE_SUBCOMMANDS:
        return None
    script = cwd / Path(words[at]).expanduser()
    return script.resolve().parents[2], words[at + 1 :]


def stale_files(tree: Path) -> list[str] | None:
    """The files `origin/master` changed since the merge base that HEAD still holds old, or None.

    None on any git failure. An empty list means HEAD holds every such file as master does.
    """
    base = _git(tree, "merge-base", "HEAD", "origin/master")
    if base is None or base.returncode != 0:
        return None
    sha = base.stdout.strip()
    changed = _git(
        tree, "diff", "--name-only", sha, "origin/master", "--", *_READ_PATHS
    )
    differs = _git(
        tree, "diff", "--name-only", "HEAD", "origin/master", "--", *_READ_PATHS
    )
    if not changed or not differs or changed.returncode or differs.returncode:
        return None
    return sorted(set(changed.stdout.split()) & set(differs.stdout.split()))


def stale_probe_problem(words: Sequence[str], cwd: Path) -> str | None:
    """`probe.py health` run from a linked worktree whose tree lags `origin/master`.

    Args:
        words: The stage's words, shell keywords stripped.
        cwd: The directory the stage runs in, after any `cd` earlier in the command.
    """
    run = _gate_run(words, cwd)
    if run is None:
        return None
    tree, args = run
    if not (tree / ".git").is_file():
        # A `.git` FILE marks a linked worktree; the primary holds a `.git` directory.
        return None
    stale = stale_files(tree)
    if not stale:
        return None
    shown = ", ".join(stale[:_SHOWN]) + (" and more" if len(stale) > _SHOWN else "")
    primary = primary_checkout(str(tree)) or "the primary checkout"
    return (
        f"This `probe.py` gate would run from {tree}, a worktree that holds an older copy of "
        f"{len(stale)} file(s) origin/master has changed since it branched ({shown}). The "
        "probe renders roles, reads owners and reads alert tables from its own tree, so a "
        "verdict here can be wrong in either direction (#6110). Run it from the primary "
        f"checkout: `cd {primary} && uv run python scripts/diagnostics/probe.py "
        f"{' '.join(args)}`, or `git rebase origin/master` in this worktree first."
    )
