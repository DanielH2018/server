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

``k8s_deferred`` is the same shape for the k8s plane: one line per promoted image bump a BROAD
tick merged and then could not apply, because the broad arm returns before the k8s arm runs. It
too leaves ``behind_since`` empty. monitor-bridge reported it from the day it existed (#2449) and
the banner did not, so a session opening on daniel-box — the reader who can clear it with one
deploy — was the one surface not told (#2470).

``k8s_unapplied`` is that same shape again for the k8s changes this deployer never applies at
all: a hand-edited role, or one of the forty denylisted ones. Nothing pages on it, by
construction — that is what lets the class have a durable record at all (#2570) — so this
banner and the deployer's journal are its only readers.

The directory, the basenames and the line parsers come from ``lib.gitops_markers``, a
generated copy of the deployer's own module (its header says how it is kept fresh), so this
module and monitor-bridge read exactly the lines the deployer wrote. What stays here is the
banner's own judgement: the thresholds, the readers that collapse an unreadable marker to
"no park", and the functions that render a marker as banner lines. Those renderers sit here
rather than in the hook because the hook is at its module-length cap and they belong beside
the readers and thresholds they consume; ``behind_park_lines`` is the exception, still in the
hook, and moving it is somebody else's change.

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
from lib.gitops_markers import (
    CONTENTION_CLEAR_CMD,
    CONTENTION_PAGE_SECONDS,
    MARKERS,
    STATE_DIR,
    k8s_deferred_clear_cmd,
    k8s_deferred_deploy_cmd,
    k8s_unapplied_clear_cmd,
    parse_contention,
    manual_plane_clear_cmd,
    maximal_apply_warning,
    parse_k8s_deferred,
    parse_manual_plane,
    parse_manual_plane_tags,
)

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


def read_manual_plane_tags_marker(state_dir: str = GITOPS_STATE_DIR) -> str | None:
    """The host's `manual_plane_tags` marker text, or None when it cannot be read.

    The sidecar naming the narrowest `--tags` value each pending role's change needs (#2307).
    Absent and unreadable collapse to the same answer for the reason the two readers above
    give, and that answer is the safe one here: a caller with no narrowing prints the
    whole-role tag, which is what every surface printed before the sidecar existed.
    `gitops_markers.parse_manual_plane_tags` turns the text into the mapping.
    """
    return _read(state_dir, MARKERS["manual_plane_tags"])


# How long a contention streak may run before the banner names it: the same number
# monitor-bridge pages on, which is why it is the shared module's and not this one's.
CONTENTION_PARK_SECONDS = CONTENTION_PAGE_SECONDS


def _age_phrase(seconds):
    """`"45 min"` under two hours, `"7h"` above it.

    Minutes match `behind_park_lines`, which never reports more than a few hours. A pending
    setup role waits on work nobody has started and is routinely days old, where a count in
    minutes is a number the reader has to divide.
    """
    # Clamped at zero: `park_age`'s threshold hid a stamp ahead of the clock, and this line has
    # no threshold, so a backward NTP step on the deployer would otherwise print a negative age.
    seconds = max(0.0, seconds)
    if seconds < 2 * 3600:
        return f"{int(seconds // 60)} min"
    return f"{int(seconds // 3600)}h"


def manual_plane_lines(marker, now, tags_marker=None):
    """One banner line per setup role the deployer merged but cannot apply, or [].

    The tick fast-forwards a range carrying `roles/setup/k3s/` or `roles/setup/common/` and
    records the role in `manual_plane` rather than parking the whole range — parking held every
    other session's landing behind one role only a hand can apply. So `behind_since` is empty
    and the park line above says nothing, while a change sits merged and unapplied.

    # DECIDED: not age-gated, unlike `behind_park_lines` and monitor-bridge's `gitops_status`.
    Being behind origin IS routine in the small — one tick — so those need a threshold to tell
    a queue from a park. A `manual_plane` entry is never routine: the tick writes it only for a
    role no tick can apply, and nothing but an operator's hand clears it. monitor-bridge gates
    because it PAGES; this is a passive notice on a banner the reader is already reading.

    The line carries the way out, because the session that reads it is usually not the session
    that landed the change: `land.sh` printed the apply command to whoever merged it, and the
    banner is the only place the fact reaches anyone else.

    `tags_marker` is the `manual_plane_tags` sidecar, so the way out names the narrowest tags
    the change needs rather than the whole-role tag (#2307). It is READ, not re-derived: an
    isolated worktree cannot ask git about the primary checkout. An absent sidecar reads as
    the role tag.

    The command carries `maximal_apply_warning` where it arms something gated (#2345). The
    journal line, the Discord alert and `land.sh` all warn through `deploy_remediation`, which
    this module cannot import — the SessionStart hook loads it with only `scripts/` on
    `sys.path`. So the warning that both surfaces key on is data in `gitops_markers`, and the
    banner was the one place printing `--tags k3s` with nothing beside it.
    """
    lines = []
    narrow = parse_manual_plane_tags(tags_marker) if tags_marker else {}
    for e in sorted(parse_manual_plane(marker), key=lambda e: e.at):
        selected = narrow.get(e.role) or {e.role}
        tags = ",".join(sorted(selected))
        warning = maximal_apply_warning(e.role, selected)
        how = (
            f"apply `{e.playbook} --tags {tags}` by hand"
            + (f" (WARNING: {warning})" if warning else "")
            if e.playbook != "none"
            else "apply the role by hand"
        )
        lines.append(
            f"  ✗ the GitOps deployer merged a change to the `{e.role}` setup role "
            f"{_age_phrase(now - e.at)} ago and cannot apply it itself — {how}, then "
            f"`{manual_plane_clear_cmd(e.role, selected)}`"
        )
    return lines


def contention_lines(marker, now):
    """One banner line while the deployer has deferred on a busy service lock for too long.

    A contention defer resets the tree and returns 0, so `last_run` advances, `hold_sha` stays
    empty and only `behind_since` ages — toward the six-hour page sized for a dirty tree. This
    names the lock and its holder's shape instead (issue #1847). Age-gated like
    `behind_park_lines`: one operator deploy holding a lock for a tick is routine.
    """
    pending = parse_contention(marker)
    if pending is None or now - pending.first_seen < CONTENTION_PARK_SECONDS:
        return []
    lock, first_seen, count = pending.lock, pending.first_seen, pending.count
    age = now - first_seen
    return [
        f"  ✗ the GitOps deployer has deferred {count} consecutive tick(s) on service lock "
        f"`{lock}` for {_age_phrase(age)} — a deploy.sh holding "
        f"/var/lock/server-deploy-{lock}.lock has outlived any legitimate deploy; find it "
        f"(fuser), end it, then `{CONTENTION_CLEAR_CMD}`"
    ]


def k8s_deferred_lines(marker, now):
    """One banner line per promoted image bump the deployer merged but did not apply, or [].

    The BROAD arm returns before the k8s arm, so a tick that fast-forwards a broad change
    alongside a promoted image bump merges the bump and never deploys it. Nothing chose that
    deferral and nothing reports it again: the defer-and-alert post fires once, and no later
    tick's range carries the bump a second time. `k8s_deferred` is the durable record, and the
    way out is one `deploy.sh` of the named service.

    # DECIDED: not age-gated, for the reason `manual_plane_lines` is not. monitor-bridge DOES
    # gate this marker (`checks/gitops.py`, 7h), and the asymmetry is deliberate rather than
    # drift: the bridge PAGES, so it needs a threshold that a tick which applies the bump on
    # its next pass clears. A banner line is a passive notice to a reader who is already
    # reading, and a bump merged ten minutes ago is exactly the one this session can still
    # clear cheaply.

    Ungated also means this says nothing about the tick that is about to apply the bump itself.
    That is the same trade `manual_plane_lines` takes and the cost is one line on one banner.
    """
    lines = []
    for entry in sorted(parse_k8s_deferred(marker), key=lambda e: e.at):
        lines.append(
            f"  ✗ the GitOps deployer merged an image bump for `{entry.service}` "
            f"{_age_phrase(now - entry.at)} ago and deferred the deploy — "
            f"`{k8s_deferred_deploy_cmd([entry.service])}`, then "
            f"`{k8s_deferred_clear_cmd(entry.service)}`"
        )
    return lines


def k8s_unapplied_lines(marker, now):
    """One banner line per k8s role change the deployer merged and will never apply, or [].

    The other half of the k8s defer-and-alert channel (#2570). A hand-edited or denylisted
    role is fast-forwarded and dropped: the Discord post fires once per SHA, no later tick's
    range carries the change, and `Release Staleness Drift` is already DOWN for any stale
    record anywhere in the fleet, so a new deferral adds nothing a reader can see on that
    tile. This banner is where the change gets named.

    # DECIDED: no age gate here, and no monitor behind it either — unlike `k8s_deferred_lines`
    # above, which is ungated on the banner while monitor-bridge pages on the same marker at
    # 7h. Forty of the fifty-four k8s roles are denylisted, so a page on this class would be
    # red as normal operation, which is the measured ground #2471 ruled the marker out on. A
    # banner line costs a reader one glance and the marker discharges itself off the release
    # record, so the set stays the changes nobody has deployed.
    """
    lines = []
    for entry in sorted(parse_k8s_deferred(marker), key=lambda e: e.at):
        lines.append(
            f"  ✗ the GitOps deployer merged a k8s change for `{entry.service}` "
            f"{_age_phrase(now - entry.at)} ago and never applies this role — "
            f"`{k8s_deferred_deploy_cmd([entry.service])}`, or "
            f"`{k8s_unapplied_clear_cmd(entry.service)}` if it was reverted"
        )
    return lines


def read_k8s_unapplied_marker(state_dir: str = GITOPS_STATE_DIR) -> str | None:
    """The host's `k8s_unapplied` marker text, or None when it cannot be read.

    Absent and unreadable collapse to the same answer, for the reason the readers above give.
    """
    return _read(state_dir, MARKERS["k8s_unapplied"])


def read_k8s_deferred_marker(state_dir: str = GITOPS_STATE_DIR) -> str | None:
    """The host's `k8s_deferred` marker text, or None when it cannot be read.

    Absent and unreadable collapse to the same answer for the reason the readers above give:
    every caller here only asks "is a bump merged and unapplied?", and for a marker nobody can
    read the answer is no. `gitops_markers.parse_k8s_deferred` turns the text into entries.
    """
    return _read(state_dir, MARKERS["k8s_deferred"])


def read_contention_marker(state_dir: str = GITOPS_STATE_DIR) -> str | None:
    """The host's `contention_since` marker text, or None when it cannot be read.

    `gitops_markers.parse_contention` turns the text into the streak.
    """
    return _read(state_dir, MARKERS["contention"])
