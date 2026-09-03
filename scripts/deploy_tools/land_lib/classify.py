"""Steps 1 and 1½: the merge commit, and what this PR reaches -- read BEFORE any wait.

A PR that reaches no service tag, no plane a hand applies and nothing the tick applies
itself has nothing to wait for: the deployer fast-forwards it on its own tick, and CI on
the merge commit is the deployer's gate, not this landing's. Sixteen of the 45 landings
before 2026-09-02 ended nothing-to-deploy after a median seven minutes of PR CI plus
master CI.
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from deploy_tools.land_lib.landing import Landing
from deploy_tools.land_lib.outcome import say


def resolve(ln: Landing) -> None:
    """Step 1: the merge commit, and a fresh origin/master in the primary checkout."""
    ln.ledger.t_merged = ln.tools.clock()
    print(f"== 1/6  resolving PR #{ln.opts.pr}")
    sha = (ln.view("mergeCommit").get("mergeCommit") or {}).get("oid") or ""
    if not sha:
        ln.die(f"PR #{ln.opts.pr} has no merge commit — is it merged?", 1)
    ln.merge_sha = sha
    ln.ledger.merge_sha = sha
    say(f"merge commit {sha}")
    ln.fetch_branch()


def pr_range(ln: Landing) -> str:
    """`<merge-base>..<pr-head>` from refs/pull/<n>/head, or '' when it cannot be read.

    `--since` covers every other session's merged work, and `MERGE_SHA^` is wrong for a
    rebase merge of a multi-commit PR (PR #843 was two commits). The pull ref's merge base
    with the merge commit is the branch point under every merge method. Any step failing
    leaves the range empty, which classifies every broad path as loud -- the direction a
    wrong answer must fall (issue #848).
    """
    if ln.git("fetch", "-q", "origin", f"refs/pull/{ln.opts.pr}/head").returncode == 0:
        head = ln.git("rev-parse", "FETCH_HEAD")
        if head.returncode == 0 and head.stdout.strip():
            base = ln.git("merge-base", head.stdout.strip(), ln.merge_sha)
            if base.returncode == 0 and base.stdout.strip():
                return f"{base.stdout.strip()}..{head.stdout.strip()}"
    say(
        f"could not read PR #{ln.opts.pr}'s own range — every broad path stays owed to a hand"
    )
    return ""


def classify(ln: Landing) -> None:
    """Tags, the plane a hand must apply, and whether the tick applies part of this PR.

    Computed whether or not tags were derived: a PR can touch a deployable role AND a
    plane, and then the deploy succeeds while half the change is unapplied.
    """
    if ln.tags:
        return
    t = ln.tools
    view = ln.view("files,changedFiles")
    paths = [f["path"] for f in view.get("files", [])]
    quiet = t.quiet_paths(paths, pr_range(ln))
    ln.plane = t.plane_note(paths, quiet=quiet)
    ln.self_applied = t.self_applied(paths, quiet=quiet)
    # -1 rather than 0: `gh` omitting the field must not read as agreement with an empty
    # file list, which would silently license a zero-tag deploy.
    tags, source = t.derive(paths, view.get("changedFiles", -1))
    ln.tags = ",".join(tags)
    if source == "fallback":
        if not ln.opts.since:
            ln.die(
                "PR file list was truncated and no --since was given — rerun with --since <pre-merge-sha>"
            )
        # The diff derivation reads `<since>...HEAD` in the primary checkout, which the tick
        # has not fast-forwarded yet. Derived in step 5, after the tick.
        ln.needs_diff = True
        say(
            f"file list truncated; deriving from the diff since {ln.opts.since} after the tick"
        )


def shortcut_if_nothing(ln: Landing) -> None:
    """A PR reaching no tag, no plane and nothing self-applied has nothing to wait for."""
    if not (ln.tags or ln.plane or ln.self_applied or ln.needs_diff):
        ln.finish(
            "nothing-to-deploy",
            0,
            f"PR #{ln.opts.pr} touched no service; the deployer fast-forwards it on its next tick",
        )
