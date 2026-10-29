"""The SessionStart banner's worktree section: remote fan-out lines and stale worktrees.

Split out of session-health.py when it reached its line cap
(ansible/tests/_ratchet.py) with no headroom left. Package name is `hooklib`, not `lib`,
so it never shares a namespace-package name with `scripts/lib` (see session-health.py's
own import comment).
"""

import json
import socket
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

# A batch launched longer ago than this is past any run's working life: an agent lands or
# stops within hours. What remains is a worktree `clean` was never run on, so the banner
# counts those rather than naming each one in every session's context.
FANOUT_LIVE_FOR = timedelta(hours=24)


def fanout_manifest_dir() -> Path:
    """Where fanout.py place writes its run manifests.

    A function, not a module constant: `Path.home()` raises `RuntimeError` when there is no
    `HOME` and no passwd entry for the uid, and at module scope that fires during the
    hook's `from hooklib.worktree_lines import ...`, which `except ImportError` does not
    catch. The whole banner then dies on a traceback over a directory it may never read.
    Resolved here instead, the raise happens at session-health.py's LAST line of work,
    where its `__main__` guard's `except Exception: sys.exit(0)` turns it into a clean exit
    with every earlier section already printed. That guard is what catches it; the fan-out
    call itself has no try/except of its own.
    """
    return Path.home() / ".claude" / "fanout"


def _batch_line(batch, run_id, me):
    """One banner line for `batch`, or None if it's on this host, cleaned, or malformed.

    Narrow except, on its own: a batch missing `host`/`branch`/`issues`, or with a
    non-list `issues`, is skipped without touching its siblings in the same manifest.

    A batch carrying `removed_at` is skipped too: `clean` took its worktree, so the line
    would advertise a directory that no longer exists on the other host, in every session
    until the last batch of the run is cleaned. An older manifest has no such key and every
    batch reads as live, which is what it was.
    """
    try:
        if batch.get("host") == me or batch.get("removed_at"):
            return None
        issues = batch["issues"]
        if not isinstance(issues, list):
            return None
        rendered = ", ".join(f"#{n}" for n in issues)
        return f"  • {batch['host']} {batch['branch']} — {rendered} (run {run_id})"
    except KeyError, AttributeError, TypeError:
        return None


def _launched_before(batch, cutoff):
    """Whether `batch` launched before `cutoff`; False when `launched_at` is absent or bad.

    An unreadable launch time reads as recent, so a batch is named in full rather than
    folded into a count on a guess.
    """
    try:
        return datetime.fromisoformat(batch["launched_at"]) < cutoff
    except KeyError, TypeError, ValueError:
        return False


def remote_fanout_lines(manifest_dir: Path | None = None, local_host=None, now=None):
    """Fan-out worktrees on the OTHER host: git worktree list here cannot see them.

    Reads the run manifests `fanout.py place` writes under ~/.claude/fanout/ instead of
    git metadata. Isolation is two levels deep: a manifest that fails to read, parse, or
    match the expected schema is skipped on its own, and within a manifest that parses, a
    malformed batch is skipped on its own -- neither discards lines already found
    elsewhere.

    Args:
        manifest_dir: run-manifest directory (a test passes `tmp_path`). Defaults to
            `fanout_manifest_dir()`, resolved here rather than at import — see that
            function on why the default cannot be a module constant.
        local_host: this host's name (a test passes a fixed value).
        now: the current time, aware (a test passes a fixed value).

    Returns:
        Ready-to-print banner lines, or [] on any read/parse error, or with no batches
        on another host. A batch launched more than `FANOUT_LIVE_FOR` ago is not named;
        one closing line per host counts those instead.
    """
    if manifest_dir is None:
        manifest_dir = fanout_manifest_dir()
    me = local_host or socket.gethostname()
    cutoff = (now or datetime.now(UTC)) - FANOUT_LIVE_FOR
    found = []
    uncleaned = Counter()
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
            if not line:
                continue
            if _launched_before(batch, cutoff):
                uncleaned[batch["host"]] += 1
            else:
                found.append(line)
    found += [
        f"  • {host}: {n} uncleaned from runs over a day old "
        "(fanout.py place runs; place clean <run-id> removes the merged ones)"
        for host, n in sorted(uncleaned.items())
    ]
    if not found:
        return []
    return [
        "fan-out worktrees on other hosts "
        "(uv run python scripts/dev/fanout.py place status <run-id>):",
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

    This is the only SessionStart pass that classifies this repo's worktrees (#6184). The
    user-level `prune-worktrees.py --prune` hook returns before classifying any repo that
    ships `scripts/dev/prune_worktrees.py`, the check its `OWN_PRUNER` constant names
    (dotfiles a0f20e92), so no second classification runs to compare against or read.

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
