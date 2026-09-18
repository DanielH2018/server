"""What the GitOps deployer's own markers say it has deferred: a park, or a pending role.

TWO READERS ASK THIS AND MUST ANSWER IT IDENTICALLY. The SessionStart banner
(``.claude/hooks/session-health.py``) reaches a session at the moment it opens.
``scripts/deploy_tools/deploy_staleness.py`` reaches a session that has been running for an
hour and hits ``deploy.sh`` exit 4 mid-landing, where the refusal names a rebase of the
reader's own worktree — the wrong repair when the primary checkout is what has to converge
(issue #1429). A second derivation of "is this a park?" would drift, and the two would then
disagree about the same marker on the same host.

``behind_since`` holds ``"<origin_sha> <unix_ts_first_seen>"`` while the host is behind
origin/master. The stamp survives a tick that moved nothing and is renewed by any tick that
fast-forwarded, so its age is HOW LONG THE DEPLOYER HAS NOT FAST-FORWARDED — not how long the
host has been behind the tip, and not how long ago the last tick ran. The distinction is load
bearing since the tick started landing at the newest green ancestor: a deployer working
normally is behind the tip on nearly every tick, and only one that stops moving ages this.

The third marker is ``contention_since``, written while consecutive ticks defer on one busy
service lock — an operator ``deploy.sh`` that never returned. The tick resets its tree on that
path, so ``behind_since`` ages toward a six-hour page sized for a dirty tree while the lock's
legitimate holder is a deploy no longer than thirty minutes (issue #1847). Same three readers
as ``manual_plane`` below.

The second marker is ``manual_plane``, one line per setup role the tick fast-forwarded past and
cannot apply itself. A recorded role leaves ``behind_since`` empty, so the park half above says
nothing while the change sits merged and unapplied — only the banner names it.

The directory, the basenames and the line parsers come from ``lib.gitops_markers``, a
generated copy of the deployer's own module (its header says how it is kept fresh), so this
module and monitor-bridge read exactly the lines the deployer wrote. What stays here is the
banner's own judgement: the thresholds, and the readers that collapse an unreadable marker to
"no park".

Stdlib plus that one generated sibling, and nothing else from this repo: the SessionStart
hook imports it with only ``scripts/`` on ``sys.path``.
"""

import os
import sys as _sys
import time
from pathlib import Path as _Path

# The generated sibling is reached as `lib.gitops_markers`, which needs `scripts/` on the path.
# The SessionStart hook and every other caller already put it there; this is for a direct
# `import lib.deployer_park` from anywhere else (the repo-root CLAUDE.md rule).
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
from lib.gitops_markers import CONTENTION_PAGE_SECONDS, MARKERS, STATE_DIR

# The deployer's marker directory on the host that runs the tick (daniel-box). Mode 0750 owned
# by `ubuntu`, so a session running as that user reads it; on any other host it is absent and
# every reader here degrades to "no park".
GITOPS_STATE_DIR = STATE_DIR

# How long `behind_since` may stand before it reads as a park rather than a queue. The tick
# runs every `gitops_deploy_tick_interval` (10 min), so 45 minutes is four ticks that all
# moved the tree nowhere — a routine push clears in one.
BEHIND_PARK_SECONDS = 45 * 60


def park_age(marker: str | None, now: float) -> float | None:
    """Seconds since the deployer last fast-forwarded, or None when this is not a park.

    Malformed or unparsable content reads as "no park": the marker is written atomically, and
    a caller that guessed an age from a torn value would be worse than one that said nothing.
    """
    if not marker:
        return None
    try:
        first_seen = float(marker.split()[-1])
    except ValueError, IndexError:
        return None
    age = now - first_seen
    return age if age >= BEHIND_PARK_SECONDS else None


def _read(state_dir: str, name: str) -> str | None:
    """One marker's stripped text, or None when it cannot be read."""
    try:
        with open(os.path.join(state_dir, name)) as fh:
            return fh.read().strip()
    except OSError:
        return None


def read_behind_marker(state_dir: str = GITOPS_STATE_DIR) -> str | None:
    """The host's `behind_since` marker text, or None when it cannot be read.

    Absent and unreadable collapse to the same answer on purpose, unlike the deployer's own
    `read_state`: every caller here only ever asks "is this a park?", and the answer to that
    for a marker nobody can read is no.
    """
    return _read(state_dir, MARKERS["behind"])


def park_note(marker: str | None, now: float | None = None) -> str:
    """The addendum `deploy.sh` exit 4 prints when the deployer is parked, or "".

    Exit 4 says "this tree is N commits behind origin/master" and points at a rebase. That is
    the right repair for an ordinary stale worktree and the wrong one for a park: the reader's
    tree is behind because the PRIMARY checkout never converged, and rebasing this worktree
    onto an origin the fleet is not running does not deploy anything.

    `now` defaults to the wall clock so a caller that has no other use for `time` does not
    import one; a test passes it explicitly.
    """
    age = park_age(marker, time.time() if now is None else now)
    if age is None:
        return ""
    return (
        f"  The GitOps deployer has ALSO not fast-forwarded for {int(age // 60)} min and is\n"
        "  behind origin/master -- that is a park, not a queue, and it is the more likely\n"
        "  reason this tree is behind. Rebasing here does not clear it: the primary checkout\n"
        "  is what has to converge.\n"
        "  Read the skip reason: journalctl -t gitops-deploy | tail -20"
    )


def read_manual_plane_marker(state_dir: str = GITOPS_STATE_DIR) -> str | None:
    """The host's `manual_plane` marker text, or None when it cannot be read.

    Absent and unreadable collapse to the same answer, for the reason `read_behind_marker`
    gives: every caller here only asks "is a role pending?", and for a marker nobody can read
    the answer is no. `gitops_markers.parse_manual_plane` turns the text into entries.
    """
    return _read(state_dir, MARKERS["manual_plane"])


# How long a contention streak may run before the banner names it: the same number
# monitor-bridge pages on, which is why it is the shared module's and not this one's.
CONTENTION_PARK_SECONDS = CONTENTION_PAGE_SECONDS


def read_contention_marker(state_dir: str = GITOPS_STATE_DIR) -> str | None:
    """The host's `contention_since` marker text, or None when it cannot be read.

    `gitops_markers.parse_contention` turns the text into the streak.
    """
    return _read(state_dir, MARKERS["contention"])
