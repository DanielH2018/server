"""What the GitOps deployer's own markers say it has deferred: a park, or a pending role.

TWO READERS ASK THIS AND MUST ANSWER IT IDENTICALLY. The SessionStart banner
(``.claude/hooks/session-health.py``) reaches a session at the moment it opens.
``scripts/deploy_tools/deploy_lib/staleness.py`` reaches a session that has been running for an
hour and hits ``deploy.sh`` exit 4 mid-landing, where the refusal names a rebase of the
reader's own worktree — the wrong repair when the primary checkout is what has to converge.
A second derivation of "is this a park?" would drift, and the two would then
disagree about the same marker on the same host.

``behind_since`` holds ``"<origin_sha> <unix_ts_first_seen>"`` while the host is behind
origin/master. The stamp survives a tick that moved nothing and is renewed by any tick that
fast-forwarded, so its age is HOW LONG THE DEPLOYER HAS NOT FAST-FORWARDED — not how long the
host has been behind the tip, and not how long ago the last tick ran. The distinction is load
bearing because the tick lands at the newest green ancestor: a deployer working normally is
behind the tip on nearly every tick, and only one that stops moving ages this.

The third marker is ``contention_since``, written while consecutive ticks defer on one busy
service lock — an operator ``deploy.sh`` that never returned. The tick resets its tree on that
path, so ``behind_since`` ages toward a six-hour page sized for a dirty tree while the lock's
legitimate holder is a deploy no longer than thirty minutes. Same three readers
as ``manual_plane`` below.

The second is the ``owed`` ledger's ``manual_plane`` class, one line per setup role the tick
fast-forwarded past and cannot apply itself. A recorded role leaves ``behind_since`` empty, so the park half above says
nothing while the change sits merged and unapplied — only the banner names it.

The directory and the line parsers come from ``gitops_markers``, the deployer's own module in
``ansible/roles/setup/gitops_deploy/files/``, and every marker is read through
``gitops_hold.read_field`` beside it (#3972), so this module and monitor-bridge read
exactly the lines the deployer wrote. What stays here is the
banner's own judgement: the thresholds, the readers that collapse an unreadable marker to
"no park", and the functions that render a marker as banner lines. Those renderers sit here
rather than in the hook because the hook is at its module-length cap and they belong beside
the readers and thresholds they consume; ``behind_park_lines`` is the exception, still in the
hook, and moving it is somebody else's change.

Stdlib plus those deployer modules and ``lib.repo_paths``, and nothing else from this repo: the
SessionStart hook imports it with only ``scripts/`` on ``sys.path``, so this module puts the
deployer's ``files/`` there itself.
"""

import sys as _sys
import time
from dataclasses import dataclass
from pathlib import Path as _Path

# `scripts/` for `lib.repo_paths`: the SessionStart hook and every other caller already put it
# there, and this is for a direct `import lib.deployer_park` from anywhere else. Then the
# deployer's `files/`, where `gitops_markers` lives (#3275).
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
from lib.repo_paths import GITOPS_DEPLOY_FILES

_sys.path.insert(0, str(GITOPS_DEPLOY_FILES))
from gitops_hold import read_field
from gitops_markers import (
    CONTENTION_CLEAR_CMD,
    CONTENTION_PAGE_SECONDS,
    PLAYBOOK_CMD,
    STATE_DIR,
    parse_contention,
    agent_join_cmds,
    agent_pin_cmds,
    by_hand,
    maximal_apply_warning,
    owed_clear_cmd,
    server_tags,
    on_hosts,
)
from gitops_ledger import (
    OWED_MANUAL_PLANE,
    manual_plane_entries,
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


def _read(state_dir: str, field: str) -> str | None:
    """One `DeployerSnapshot` field via `gitops_hold.read_field`, or None when unreadable.

    One marker at a time, so a damaged sibling cannot hide the park this one records.
    """
    try:
        return read_field(state_dir, field)
    except OSError, UnicodeDecodeError:
        return None


def _owed(state_dir: str) -> str | None:
    """The `owed` ledger's decodable lines, or None when absent, empty or unreadable."""
    return _read(state_dir, "owed")


def read_behind_marker(state_dir: str = GITOPS_STATE_DIR) -> str | None:
    """The host's `behind_since` marker text, or None when it cannot be read.

    Absent and unreadable collapse to the same answer on purpose, unlike the deployer's own
    `read_state`: every caller here only ever asks "is this a park?", and the answer to that
    for a marker nobody can read is no.
    """
    return _read(state_dir, "behind")


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
        "  Read the skip reason: journalctl -u gitops-deploy.service -n 20"
    )


def read_manual_plane_marker(state_dir: str = GITOPS_STATE_DIR) -> str | None:
    """The host's `owed` ledger text, which holds the pending setup roles, or None when unread.

    The `manual_plane` class lives in the `owed` ledger (#3392). Absent and unreadable collapse
    to the same answer, for the reason `read_behind_marker` gives: every caller here only asks
    "is a role pending?", and for a ledger nobody can read the answer is no.
    `gitops_ledger.manual_plane_entries` turns the text into entries and their tags.
    """
    return _owed(state_dir)


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


@dataclass(frozen=True)
class ManualPlaneRemedy:
    """One owed `manual_plane` role and the commands that discharge it.

    Attributes:
        role: the setup role.
        age: how long ago the deployer recorded it, worded by `_age_phrase`.
        apply: the server-play command that applies the role, or "" for a role no printed
            playbook applies or a row only `agents` applies (#5628).
        by_hand: what to do instead of `apply` for such a role, else "".
        warning: what `apply` arms beyond the change, or "".
        agents: the join-play commands that apply the role's agent half after `apply`, from
            `agent_join_cmds`, or () for a role or a row with no agent half (#5627).
        pins: the pin-play commands, as `(host to run it on, command)`, from
            `agent_pin_cmds`, or () for a row that does not reach the pin play (#5918).
        clear: the `clear-owed` command to run once the role is applied.
        tags: the tags every command above selects: the line's narrowed row, or the role's own
            tag for a whole-role line. `gitops_state.py run-owed` clears exactly these.
    """

    role: str
    age: str
    apply: str
    by_hand: str
    warning: str
    agents: tuple[str, ...]
    pins: tuple[tuple[str, str], ...]
    clear: str
    tags: frozenset[str] = frozenset()


def manual_plane_remedies(owed, now) -> list[ManualPlaneRemedy]:
    """The way out for each setup role the deployer merged but cannot apply, oldest first.

    `owed` is the deployer's ledger text. Each role's `tags` key lets the way out name the
    narrowest tags the change needs rather than the whole-role tag (#2307). It is READ, not
    re-derived: an isolated worktree cannot ask git about the primary checkout. A role with no
    tags reads as the role tag.

    The command carries `maximal_apply_warning` where it arms something gated (#2345). The
    journal line, the Discord alert and `land.sh` all warn through `deploy_remediation`, which
    this module cannot import — the SessionStart hook loads it with only `scripts/` on
    `sys.path`. So the warning that both surfaces key on is data in `gitops_markers`, and the
    banner was the one place printing `--tags k3s` with nothing beside it.
    """
    remedies = []
    entries, narrow = manual_plane_entries(owed)
    for e in entries:
        selected = narrow.get(e.role) or {e.role}
        server = server_tags(e.role, selected)
        hand = by_hand(e.playbook) or ""
        apply = (
            ""
            if hand or not server
            else on_hosts(
                f"{PLAYBOOK_CMD} {e.playbook} --tags {','.join(sorted(server))}",
                e.apply_hosts(),
            )
        )
        remedies.append(
            ManualPlaneRemedy(
                role=e.role,
                age=_age_phrase(now - e.at),
                apply=apply,
                by_hand=hand,
                warning=maximal_apply_warning(e.role, server) if apply else "",
                agents=tuple(
                    agent_join_cmds(e.role, e.playbook, selected, e.agent_hosts)
                    if not hand
                    else ()
                ),
                pins=tuple(
                    agent_pin_cmds(e.role, e.playbook, selected, e.agent_hosts)
                    if not hand
                    else ()
                ),
                clear=owed_clear_cmd(OWED_MANUAL_PLANE, e.role, selected),
                tags=frozenset(selected),
            )
        )
    return remedies


def manual_plane_lines(owed, now):
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

    The line carries the way out from `manual_plane_remedies`, because the session that reads
    it is usually not the session that landed the change: `land.sh` printed the apply command
    to whoever merged it, and the banner is the only place the fact reaches anyone else.
    """
    lines = []
    for r in manual_plane_remedies(owed, now):
        if r.by_hand:
            how = r.by_hand
        elif r.apply:
            how = (
                f"apply `{r.apply}` by hand"
                + (f" (WARNING: {r.warning})" if r.warning else "")
                + "".join(f" and `{a}`" for a in r.agents)
                + "".join(f" and `{cmd}` (run on {host})" for host, cmd in r.pins)
            )
        else:
            how = "apply " + " and ".join(f"`{a}`" for a in r.agents) + " by hand"
        lines.append(
            f"  ✗ the GitOps deployer merged a change to the `{r.role}` setup role "
            f"{r.age} ago and cannot apply it itself — {how}, then `{r.clear}`"
        )
    return lines


def contention_lines(marker, now):
    """One banner line while the deployer has deferred on a busy service lock for too long.

    A contention defer resets the tree and returns 0, so `last_run` advances, `hold_sha` stays
    empty and only `behind_since` ages — toward the six-hour page sized for a dirty tree. This
    names the lock and its holder's shape instead. Age-gated like
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


def read_contention_marker(state_dir: str = GITOPS_STATE_DIR) -> str | None:
    """The host's `contention_since` marker text, or None when it cannot be read.

    `gitops_markers.parse_contention` turns the text into the streak.
    """
    return _read(state_dir, "contention")
