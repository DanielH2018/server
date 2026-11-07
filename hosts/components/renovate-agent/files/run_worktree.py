#!/usr/bin/env python3
"""The run worktree's lifecycle: decide whether it can be recreated, then recreate it.

The session runs in a dedicated worktree, never in the primary checkout. One untracked file
in /home/<user>/server parks the GitOps deployer silently, and a session that edits, renders
and tests is guaranteed to leave some.

Everything here talks to git and to the forge, and to nothing the census and digest halves of
`renovate_agent.py` touch. It names that module nowhere, so the dependency runs one way:
`renovate_agent` imports `prepare_worktree` and `worktree_is_reusable` from here, and both
modules take their process boundaries from `agent_toolbox`.

Stdlib only, like the rest of the agent.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from agent_toolbox import TOOLS, AgentTools, log, read_file


def is_registered_worktree(repo_dir: str, path: str, tools: AgentTools = TOOLS) -> bool:
    """Whether git actually knows about the directory at `path`.

    A directory can sit at the run worktree's path with no git metadata at all — that is what a
    killed session leaves behind, and it is what stopped this agent for two days from
    2026-09-08 (#1477). It matters because **git searches UPWARD for a repository**: a
    `git -C <orphan dir> status` resolves to the PRIMARY CHECKOUT and answers about that tree
    instead, so every check made from inside such a directory is about the wrong repo.

    Compared by resolved path, since `git worktree list --porcelain` prints the real path and
    `repo_dir` may be reached through a symlink.
    """
    rc, out = tools.run(["git", "-C", repo_dir, "worktree", "list", "--porcelain"])
    if rc != 0:
        # Fail closed: unknown means "treat it as git's", so nothing removes a tree that may be
        # registered. The caller's own error path then names the path for an operator.
        return True
    want = os.path.realpath(path)
    return any(
        os.path.realpath(line[len("worktree ") :].strip()) == want
        for line in out.splitlines()
        if line.startswith("worktree ")
    )


def worktree_is_reusable(
    repo_dir: str, path: str, branch: str, tools: AgentTools = TOOLS, repo: str = ""
) -> tuple[bool, str]:
    """Whether the run worktree can be thrown away and recreated.

    It cannot when the previous tick left work behind — uncommitted changes, or commits that
    never landed (branch_content_is_on_master decides that, since a squash merge leaves the
    commits themselves unreachable from master; `repo` is the GitHub slug it asks about).
    Removing either would destroy a landing that was in flight, so the tick skips instead and
    names the path for the operator.

    SCOPE (see lib.git.git_dirty, #1223): whole tree, untracked counted — an unlanded scratch
    file is exactly the kind of leftover this must not discard. Stays inline rather than
    importing lib.git because this file is deployed onto the host with no path to scripts/lib.
    """
    if not os.path.isdir(path):
        return True, ""
    if not is_registered_worktree(repo_dir, path, tools):
        # Debris, not a worktree: `git -C path status` would search upward and report on the
        # PRIMARY CHECKOUT, so a dirty primary would read as "this run tree has uncommitted
        # changes" and a clean one would wave through a directory `worktree add` then refuses.
        # It holds no work git can see, so prepare_worktree reclaims it.
        return True, ""
    rc, out = tools.run(["git", "-C", path, "status", "--porcelain"])
    if rc != 0:
        return False, f"git status failed in {path}: {out.strip()[:200]}"
    if out.strip():
        return False, f"{path} has uncommitted changes from an earlier run"
    rc, out = tools.run(
        ["git", "-C", repo_dir, "rev-list", "--count", f"origin/master..{branch}"]
    )
    if rc == 0 and out.strip() not in ("0", ""):
        if branch_content_is_on_master(repo_dir, branch, tools, repo):
            return True, ""
        return False, f"{branch} holds {out.strip()} commit(s) not on origin/master"
    return True, ""


def branch_content_is_on_master(
    repo_dir: str, branch: str, tools: AgentTools = TOOLS, repo: str = ""
) -> bool:
    """Whether `branch` has already landed, by content or by the forge's record.

    Ancestry alone cannot see a squash merge: the landing keeps the content and discards the
    commits that carried it, so `rev-list origin/master..<branch>` counts them forever. The
    run worktree's fixed branch is never reset after a landing, so from 2026-09-14 the tick
    refused its own tree every day while its content sat on master as PR #1812 (#2014).
    `git merge-tree --write-tree` asks about content instead: when the tree it would produce
    is origin/master's own tree, the branch has nothing master lacks and the worktree can be
    recreated. Once master has drifted into a conflict on a file the branch touched, that
    exits non-zero — the very tree #2014 found was already in that state — so the forge is
    asked last whether it merged a PR from exactly this tip (`repo`, the `owner/name` slug,
    is what `gh` needs; empty means no forge check). Inlined from merge_tree_says_contained
    and pr_head_says_merged in the dotfiles-deployed `claude_worktree` package
    (scripts/lib/_claude_worktree.py says where it lives) for the reason in
    worktree_is_reusable's docstring — this file ships with no path to scripts/ or to it.

    DECIDED: no verdict reads as NOT contained. A non-zero merge-tree exit, empty output, an
    unreadable master tree, and a `gh` that fails or names no PR at this tip all refuse,
    because a wrong yes here deletes work. A revert-only branch is refused for the same
    reason: merging it changes master's tree, so it still holds something master lacks. The
    forge match is on the head SHA, never on the branch name: the name is reused every tick.
    """
    rc, master_tree = tools.run(
        ["git", "-C", repo_dir, "rev-parse", "origin/master^{tree}"]
    )
    if rc != 0 or not master_tree.strip():
        return False
    rc, merged = tools.run(
        ["git", "-C", repo_dir, "merge-tree", "--write-tree", "origin/master", branch],
        timeout=300,
    )
    lines = [line.strip() for line in merged.splitlines() if line.strip()]
    if rc == 0 and lines and lines[0] == master_tree.strip():
        return True
    return branch_tip_was_merged(repo_dir, branch, repo, tools)


def branch_tip_was_merged(
    repo_dir: str, branch: str, repo: str, tools: AgentTools = TOOLS
) -> bool:
    """Whether the forge merged a PR whose head was exactly `branch`'s current tip."""
    if not repo:
        return False
    rc, tip = tools.run(["git", "-C", repo_dir, "rev-parse", branch])
    if rc != 0 or not tip.strip():
        return False
    rc, out = tools.run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "merged",
            "--head",
            branch,
            "--json",
            "headRefOid",
        ]
    )
    if rc != 0:
        return False
    try:
        prs = json.loads(out or "[]")
    except json.JSONDecodeError:
        return False
    if not isinstance(prs, list):
        return False
    return any(isinstance(p, dict) and p.get("headRefOid") == tip.strip() for p in prs)


def _process_start_time(pid: int) -> str:
    """The starttime field `claude_worktree.session_is_alive()` compares.

    Read from /proc/<pid>/stat: starttime is the field after the last ')', at index 19 once
    split on whitespace — the comm field can itself contain spaces or parens, which is why
    session_is_alive() anchors on the final ')' rather than counting from the start. Returns
    "" if the pid can't be read, which makes the lock reason fail LOCK_OWNER's regex and so
    read as "someone else's format" — session_is_alive() treats that as alive, never as dead.
    """
    stat = read_file(f"/proc/{pid}/stat")
    fields = stat.rpartition(")")[2].split()
    return fields[19] if len(fields) > 19 else ""


def _lock_reason() -> str:
    """The reason string `git worktree lock` records, in the format LOCK_OWNER parses.

    scripts/lib/worktrees.py's classify() keeps a worktree only while `tree.locked and
    session_is_alive(tree.lock_reason)`, and `prune_worktrees.py --prune` removes only what it
    does not keep. `claude_worktree.session_is_alive()` matches `(pid <n> start <n>)`
    against /proc/<pid>/stat, so this must be this process's own pid and start time. As long
    as this script is still running (it blocks on run_session() for the run's duration), the
    pid+start pair keeps matching and the pruner keeps the tree; once the process exits, the
    pid no longer matches (or is reused with a different start time) and the lock is ignored.
    """
    pid = os.getpid()
    return f"renovate-agent (pid {pid} start {_process_start_time(pid)})"


def prepare_worktree(
    repo_dir: str, path: str, branch: str, tools: AgentTools = TOOLS
) -> None:
    """Recreate the run worktree at origin/master. Assumes worktree_is_reusable said yes."""
    if os.path.isdir(path):
        if is_registered_worktree(repo_dir, path, tools):
            # The previous tick's lock is still on this tree — `worktree remove` refuses a
            # locked tree outright, and passing --force once does not override a lock (git
            # needs it twice). Unlock first, mirroring scripts/lib/worktrees.py's remove().
            tools.run(["git", "-C", repo_dir, "worktree", "unlock", path])
            tools.run(["git", "-C", repo_dir, "worktree", "remove", "--force", path])
        else:
            # An unregistered directory is a recoverable state, not a crash. `worktree remove`
            # refuses a path it has no record of, the return code was discarded, and the
            # following `worktree add` then died on `fatal: ... already exists` — which killed
            # every tick from 2026-09-08 to 2026-09-10 and left the alive monitor to expire
            # (#1477). git holds no record of it, so there is no branch and no commit to lose;
            # `worktree_is_reusable` above has already refused every case that does.
            log(f"reclaiming {path}: a directory git has no worktree record of")
            tools.rmtree(path, ignore_errors=True)
            if os.path.isdir(path):
                raise RuntimeError(f"could not remove the orphaned directory {path}")
    tools.run(["git", "-C", repo_dir, "worktree", "prune"])
    rc, out = tools.run(
        ["git", "-C", repo_dir, "fetch", "--quiet", "origin", "master"], timeout=300
    )
    if rc != 0:
        raise RuntimeError(f"git fetch failed: {out.strip()[:300]}")
    rc, out = tools.run(
        ["git", "-C", repo_dir, "worktree", "add", "-B", branch, path, "origin/master"],
        timeout=300,
    )
    if rc != 0:
        raise RuntimeError(f"git worktree add failed: {out.strip()[:300]}")
    # Lock the tree for the run's duration so a concurrent session's SessionStart pruner
    # (prune_worktrees.py --prune) doesn't delete it out from under this run — see #1069.
    rc, out = tools.run(
        ["git", "-C", repo_dir, "worktree", "lock", path, "--reason", _lock_reason()]
    )
    if rc != 0:
        raise RuntimeError(f"git worktree lock failed: {out.strip()[:300]}")
    # The repo's hooks run through `uv run --no-sync` in this tree, which the unit names as
    # RUN_HOOK_PROJECT_DIR, and a fresh worktree has no `.venv`. A guard that cannot import its
    # dependencies raises, and the hook dispatcher drops a raising guard's verdict in silence.
    rc, out = tools.run(["uv", "sync", "--frozen", "--quiet"], cwd=path, timeout=300)
    if rc != 0:
        raise RuntimeError(f"uv sync failed: {out.strip()[:300]}")
