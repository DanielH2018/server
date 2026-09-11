"""Whether the GitOps deployer is PARKED behind origin/master, read from its own marker.

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

Stdlib only, and no imports from this repo: the SessionStart hook imports it before anything
else is on ``sys.path``.
"""

import os
import time

# The deployer's marker directory on the host that runs the tick (daniel-box). Mode 0750 owned
# by `ubuntu`, so a session running as that user reads it; on any other host it is absent and
# every reader here degrades to "no park".
GITOPS_STATE_DIR = "/var/lib/gitops-deploy"

BEHIND_SINCE = "behind_since"

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
    return _read(state_dir, BEHIND_SINCE)


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


# The deployer's `manual_plane` marker: one line per setup role it fast-forwarded past and
# cannot apply itself, `"<origin_sha> <playbook-or-none> <role> <unix_ts>"`.
MANUAL_PLANE = "manual_plane"

# What an operator runs to clear one pending role once the role is applied by hand. The
# deployer's own copy is `deploy_remediation.MANUAL_PLANE_CLEAR_CMD`; this module is stdlib
# only and cannot import that tree, so
# `scripts/lib/tests/test_manual_plane_parsers_agree.py` asserts the two agree.
MANUAL_PLANE_CLEAR_CMD = (
    "uv run python scripts/deploy_tools/gitops_state.py clear-manual-plane <role>"
)


def manual_plane_pending(marker: str | None) -> list[tuple[str, str, float]]:
    """Every pending role in the marker as `(role, playbook, first_seen)`, oldest line first.

    A line this cannot parse is SKIPPED rather than guessed at, the way `park_age` treats a
    garbled `behind_since`: the banner names a role and a command to clear it, and neither can
    be derived from a torn line. `DeployerState.manual_plane_pending` and monitor-bridge's
    `checks.service._parse_manual_plane` skip the same lines for the same reason — that
    agreement is a test, not a coincidence.

    `playbook` is the literal marker field, which the deployer writes as `none` when no
    playbook applies the role.
    """
    pending = []
    for line in (marker or "").splitlines():
        parts = line.split()
        if len(parts) != 4:
            continue
        try:
            at = float(parts[3])
        except ValueError:
            continue
        pending.append((parts[2], parts[1], at))
    return pending


def read_manual_plane_marker(state_dir: str = GITOPS_STATE_DIR) -> str | None:
    """The host's `manual_plane` marker text, or None when it cannot be read.

    Absent and unreadable collapse to the same answer, for the reason `read_behind_marker`
    gives: every caller here only asks "is a role pending?", and for a marker nobody can read
    the answer is no.
    """
    return _read(state_dir, MANUAL_PLANE)
