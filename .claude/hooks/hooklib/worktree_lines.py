"""The SessionStart banner's worktree section: remote fan-out lines and stale worktrees.

Split out of session-health.py, which sits at its own 600-line cap
(ansible/tests/_ratchet.py) with no headroom left. Package name is `hooklib`, not `lib`,
so it never shares a namespace-package name with `scripts/lib` (see session-health.py's
own import comment).
"""

import json
import socket
from pathlib import Path

FANOUT_MANIFEST_DIR = Path.home() / ".claude" / "fanout"


def _batch_line(batch, run_id, me):
    """One banner line for `batch`, or None if it's on this host or malformed.

    Narrow except, on its own: a batch missing `host`/`branch`/`issues`, or with a
    non-list `issues`, is skipped without touching its siblings in the same manifest.
    """
    try:
        if batch.get("host") == me:
            return None
        issues = batch["issues"]
        if not isinstance(issues, list):
            return None
        rendered = ", ".join(f"#{n}" for n in issues)
        return f"  • {batch['host']} {batch['branch']} — {rendered} (run {run_id})"
    except KeyError, AttributeError, TypeError:
        return None


def remote_fanout_lines(manifest_dir=FANOUT_MANIFEST_DIR, local_host=None):
    """Fan-out worktrees on the OTHER host: git worktree list here cannot see them.

    Reads scripts/dev/fanout_place.py's run manifests under ~/.claude/fanout/ instead of
    git metadata. Isolation is two levels deep: a manifest that fails to read, parse, or
    match the expected schema is skipped on its own, and within a manifest that parses, a
    malformed batch is skipped on its own -- neither discards lines already found
    elsewhere.

    Args:
        manifest_dir: run-manifest directory (a test passes `tmp_path`).
        local_host: this host's name (a test passes a fixed value).

    Returns:
        Ready-to-print banner lines, or [] on any read/parse error, or with no batches
        on another host.
    """
    me = local_host or socket.gethostname()
    found = []
    for path in sorted(manifest_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text())
            run_id = data["run_id"]
            batches = data.get("batches", [])
        except OSError, ValueError, KeyError, AttributeError, TypeError:
            continue
        if not isinstance(batches, list):
            continue
        for batch in batches:
            line = _batch_line(batch, run_id, me)
            if line:
                found.append(line)
    if not found:
        return []
    return [
        "\U0001f6f0 fan-out worktrees on other hosts "
        "(uv run python scripts/dev/fanout_place.py status <run-id>):",
        *found,
    ]


def _stale_worktree_lines(run, timeout):
    """Merged worktrees this repo can remove, as ready-to-print banner lines.

    Claude Code's own worktree keeper reports these too, but each of its lines ends by
    asking the reader to run `gh pr list --state merged --head <branch>` by hand to tell a
    squash-merged branch from one that is merely behind. prune_worktrees.py already makes
    that call, so this prints its verdict instead. Bounded and skipped on any failure, like
    every other check here -- it reaches GitHub, and a slow API must never stall session
    start.

    Args:
        run: the subprocess runner (session-health.py's `_run`, passed in rather than
            imported back -- this module cannot import from the hook that imports it).
        timeout: seconds before the subprocess is abandoned.
    """
    try:
        proc = run(
            ["uv", "run", "python", "scripts/dev/prune_worktrees.py", "--brief"],
            timeout,
        )
    except Exception:
        return []
    return [line for line in proc.stdout.splitlines() if line.strip()]
