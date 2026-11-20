"""The SessionStart banner's worktree section: remote fan-out lines and stale worktrees.

Split out of session-health.py when it reached its line cap
(tests/_ratchet.py) with no headroom left. Package name is `hooklib`, not `lib`,
so it never shares a namespace-package name with `scripts/lib` (see session-health.py's
own import comment).
"""

import json
import os
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


def _remote_host(batch, me):
    """The host `batch` runs on, or None if it's on this host, cleaned, or malformed.

    Narrow except, on its own: a batch missing `host`/`branch`/`issues`, or with a
    non-list `issues`, is skipped without touching its siblings in the same manifest.

    A batch carrying `removed_at` is skipped too: `clean` took its worktree, so counting it
    would advertise a directory that no longer exists on the other host, in every session
    until the last batch of the run is cleaned. An older manifest has no such key and every
    batch reads as live, which is what it was.
    """
    try:
        if batch.get("host") == me or batch.get("removed_at"):
            return None
        if not isinstance(batch["issues"], list) or not batch["branch"]:
            return None
        return batch["host"]
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
        on another host. Each run gets one line counting its batches on each host, since
        `place status <run-id>` lists them. A batch launched more than `FANOUT_LIVE_FOR`
        ago is counted on one closing line per host instead.
    """
    if manifest_dir is None:
        manifest_dir = fanout_manifest_dir()
    me = local_host or socket.gethostname()
    cutoff = (now or datetime.now(UTC)) - FANOUT_LIVE_FOR
    live = Counter()
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
            host = _remote_host(batch, me)
            if not host:
                continue
            if _launched_before(batch, cutoff):
                uncleaned[host] += 1
            else:
                live[host, run_id] += 1
    found = [
        f"  • {host}: {n} batch{'es' if n != 1 else ''} in run {run_id}"
        for (host, run_id), n in live.items()
    ]
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


# The dotfiles pruner interface this banner was written for. `scripts/lib/_claude_worktree.py`
# holds the same number for the importers, and
# `.claude/hooks/tests/test_session_health.py::test_the_banner_requires_the_interface_the_library_does`
# holds the two equal.
PRUNER_INTERFACE = 2


def pruner_path() -> Path:
    """The dotfiles worktree pruner, in the user-level hooks directory `chezmoi apply` fills."""
    config = os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude"
    return Path(config) / "hooks" / "prune-worktrees.py"


def _stale_worktree_lines(run, timeout):
    """Merged worktrees this repo can remove, as ready-to-print banner lines.

    Claude Code's own worktree keeper reports these too, but each of its lines ends by
    asking the reader to run `gh pr list --state merged --head <branch>` by hand to tell a
    squash-merged branch from one that is merely behind. The dotfiles pruner makes
    that call, so this prints its `--brief` verdict instead. Bounded, like every other
    check here: it reaches GitHub, and a slow API must never stall session start.

    This is the only SessionStart pass that classifies this repo's worktrees. The pruner's
    own SessionStart sweep stays out of this repo, because `.claude/prune-worktrees.json`
    says `"session_start": "off"`.

    A host with no pruner, or one older than `PRUNER_INTERFACE`, gets a line saying so. An
    empty banner would read as "nothing to remove".

    Args:
        run: the subprocess runner (session-health.py's `_run`, passed in rather than
            imported back -- this module cannot import from the hook that imports it).
        timeout: seconds before the subprocess is abandoned.
    """
    pruner = str(pruner_path())
    try:
        proc = run([pruner, "--require", str(PRUNER_INTERFACE), "--brief"], timeout)
    except FileNotFoundError:
        return [f"  ⚠ no worktree pruner at {pruner}: `chezmoi apply` deploys it"]
    except Exception:
        return []
    if proc.returncode != 0:
        why = (proc.stderr.strip().splitlines() or [f"exit {proc.returncode}"])[-1]
        return [f"  ⚠ the worktree pruner failed: {why}"]
    return [line for line in proc.stdout.splitlines() if line.strip()]
