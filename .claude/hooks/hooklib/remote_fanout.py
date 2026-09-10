"""Read scripts/dev/fanout_place.py's run manifests for the SessionStart banner.

Split out of session-health.py, which sits at its own 600-line cap
(ansible/tests/_ratchet.py) with no headroom left. Package name is `hooklib`, not `lib`,
so it never shares a namespace-package name with `scripts/lib` (see session-health.py's
own import comment).
"""

import json
import socket
from pathlib import Path

FANOUT_MANIFEST_DIR = Path.home() / ".claude" / "fanout"


def remote_fanout_lines(manifest_dir=FANOUT_MANIFEST_DIR, local_host=None):
    """Fan-out worktrees on the OTHER host: git worktree list here cannot see them.

    Reads scripts/dev/fanout_place.py's run manifests under ~/.claude/fanout/ instead of
    git metadata. Each manifest is read on its own: one that fails to read, parse, or
    match the expected schema is skipped, never discarding lines already found in others.

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
            for b in data.get("batches", []):
                if b.get("host") != me:
                    issues = ", ".join(f"#{n}" for n in b.get("issues", []))
                    found.append(
                        f"  • {b['host']} {b['branch']} — {issues} (run {data['run_id']})"
                    )
        except OSError, ValueError, KeyError, AttributeError, TypeError:
            continue
    if not found:
        return []
    return [
        "\U0001f6f0 fan-out worktrees on other hosts "
        "(uv run python scripts/dev/fanout_place.py status <run-id>):",
        *found,
    ]
