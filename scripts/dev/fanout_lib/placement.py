"""Pure placement over (host, cap, current, live_agents) — spec 2026-09-06 §2.

Decides; does not fetch. The one read it needs is transport.HOST_READ_COMMAND — READ_COMMAND's
memory lines plus the signing-key line transport appends — run by transport.read_host.
"""

from collections.abc import Sequence
from dataclasses import dataclass

# DECIDED: 2.5 GiB per agent. daniel-box's 24h peak for user-1000.slice was 7.2 GiB
# (claude_code defaults/main.yml derivation) with three to four `claude` processes live, so
# ~2 GiB each at peak; 2.5 GiB adds a pytest fan-out's worth (4 workers x ~150 MB). Confirm
# against claude_cgroup_memory_current_bytes / claude_cgroup_pids_current before raising.
RESERVATION_BYTES = 2_684_354_560

# SIX lines, in this fixed order: user.slice's memory.current and memory.high, then
# user-1000.slice's memory.current and memory.high, then the count of live `claude` processes
# for uid 1000, then the host's commit-signing key. Either memory.high is an integer, or
# `max` when no drop-in caps it. `pgrep -c` exits 1 on a zero count, which is why the count
# is read from stdout rather than from an exit status.
#
# READ_COMMAND itself emits the first five; transport.HOST_READ_COMMAND appends the signing
# key. That line rides this same read because the ssh budget (2 reads plus
# MAX_BATCHES_PER_REMOTE_HOST launches = the 5 `ufw limit ssh` allows per 30s) has no room
# for a connection of its own. It goes LAST so the numeric fields keep fixed indices and a
# key line — which carries spaces, and on a misconfigured host many lines — can never be
# read as one of them.
#
# Both slices are read because an agent is throttled by both. A launch starts a transient
# user service, which systemd places in user-1000.slice (the login plane, capped by
# claude_code_rc_memory_high), nested under user.slice (the fleet, capped by
# claude_code_fleet_memory_high). Reading the fleet alone picks a host whose 12G/10G fleet
# cap has room while the 8G cap that actually binds the agent is full: daniel-box's login
# slice peaked at 8.58 GB against that 8G cap on 2026-09-10.
READ_COMMAND = (
    "cat /sys/fs/cgroup/user.slice/memory.current /sys/fs/cgroup/user.slice/memory.high "
    "/sys/fs/cgroup/user.slice/user-1000.slice/memory.current "
    "/sys/fs/cgroup/user.slice/user-1000.slice/memory.high; "
    "pgrep -c -x claude -u 1000"
)


@dataclass(frozen=True)
class HostReading:
    """One host's memory-cgroup reading: both caps, both current usages, live agent count.

    Attributes:
        host: the host the reading came from.
        cap_bytes: the fleet cap — user.slice memory.high, or None when no drop-in caps it.
        current_bytes: user.slice memory.current.
        plane_cap_bytes: the login-plane cap — user-1000.slice memory.high, or None when no
            drop-in caps it. An agent runs as a transient user service inside that slice, so
            this bounds it as surely as the fleet cap above does.
        plane_current_bytes: user-1000.slice memory.current.
        live_agents: how many `claude` processes uid 1000 is running. Read and reported —
            `read` prints it and NoHeadroom names it — but never scored: placement decides
            on headroom alone (spec §2), because a host's agents are already priced into
            the memory the reading measures.
        signing_key: what the host's signing-key read printed, carried for the launch gate
            in fanout_place (fanout_lib.signing) and, like live_agents, never scored here.
    """

    host: str
    cap_bytes: int | None
    current_bytes: int
    plane_cap_bytes: int | None
    plane_current_bytes: int
    live_agents: int
    # Defaults empty, which `signing.unverified_reason` refuses: a reading built without a
    # key — a test double, or a future caller that forgets it — must fail the gate closed
    # rather than pass it for want of a value.
    signing_key: str = ""


class NoHeadroom(Exception):
    """Neither host can take the batch; str() carries every host's numbers."""


def parse_reading(host: str, stdout: str) -> HostReading:
    """Parse transport.HOST_READ_COMMAND's six-line output into a HostReading.

    The line order is READ_COMMAND's, with the signing key last: fleet current, fleet high,
    plane current, plane high, live-agent count, signing key.

    A host still answering either older shape — three lines before the plane read, four
    before it — is a parse error naming the host, not a reading with the missing half
    guessed at. The read and this parser ship together, and a fabricated plane cap is the bad
    placement the plane read exists to prevent, while a fabricated key is a gate that passes
    everything.

    Args:
        host: the host the reading came from.
        stdout: the command's stdout — user.slice's memory.current and memory.high (or
            `max`), user-1000.slice's memory.current and memory.high (or `max`), the
            live-agent count, then the host's signing key, one per line.

    Returns:
        The parsed reading.

    Raises:
        ValueError: the output is not exactly six non-blank lines, or a numeric field does
            not parse as an integer. The message names the line count and never the output:
            `user.signingkey` can point at a private key file, and a read on a host
            configured that way would put its contents in this message.
    """
    lines = [ln.strip() for ln in stdout.splitlines() if ln.strip()]
    if len(lines) != 6:
        raise ValueError(
            "%s: expected 6 lines from the host read, got %d" % (host, len(lines))
        )
    current, high, plane_current, plane_high, agents, signing_key = lines
    cap = None if high == "max" else int(high)
    plane_cap = None if plane_high == "max" else int(plane_high)
    return HostReading(
        host, cap, int(current), plane_cap, int(plane_current), int(agents), signing_key
    )


def _headrooms(reading: HostReading, reservation: int) -> dict[str, int]:
    """Bytes free after one reservation, per capped side, keyed by that side's name.

    A side whose cap read `max` does not bound the host and is left out, so an entirely
    uncapped host maps to nothing at all.
    """
    sides = (
        ("fleet", reading.cap_bytes, reading.current_bytes),
        ("login-plane", reading.plane_cap_bytes, reading.plane_current_bytes),
    )
    return {
        name: cap - current - reservation
        for name, cap, current in sides
        if cap is not None
    }


def headroom(reading: HostReading, reservation: int = RESERVATION_BYTES) -> int | None:
    """Bytes free under the TIGHTER of the two caps after one reservation, or None.

    An agent is throttled by whichever of the fleet cap (user.slice) and the login-plane cap
    (user-1000.slice) it reaches first, so the smaller headroom is the host's real headroom.
    One reservation, not two: the agent's memory counts against both cgroups at once, since
    one is the other's parent. A cap read as `max` does not bound that side and drops out of
    the comparison; when neither side is capped the host is uncapped and returns None, which
    `place` reads as "not a candidate" — nothing bounds an agent there.
    """
    rooms = _headrooms(reading, reservation)
    return min(rooms.values()) if rooms else None


def _describe(
    readings: Sequence[HostReading], used: dict[str, int], reservation: int
) -> str:
    # Names the side the number came from. It is a min() over two caps now, so a bare "0.7
    # GiB free" against a fleet an operator knows has 5.3 GiB free reads as a broken tool —
    # and this string is the whole of what an exit 3 tells them.
    parts = []
    for r in readings:
        rooms = _headrooms(r, reservation)
        if rooms:
            side = min(rooms, key=lambda name: rooms[name])
            shown = "%.1f GiB free under its %s cap" % (
                (rooms[side] - used[r.host]) / 1024**3,
                side,
            )
        else:
            shown = "uncapped"
        parts.append("%s: %s (%d live agents)" % (r.host, shown, r.live_agents))
    return "; ".join(parts)


def place(
    batches: Sequence[str],
    readings: Sequence[HostReading],
    pin: str | None = None,
    reservation: int = RESERVATION_BYTES,
) -> list[tuple[str, str]]:
    """Assign each batch a host, most headroom first, spending one reservation per placement.

    Args:
        batches: the batch names to place, in order.
        readings: the current headroom reading for every candidate host.
        pin: when given, place every batch on this host and refuse rather than fall back
            when it runs out of headroom.
        reservation: bytes reserved per placed batch (default RESERVATION_BYTES).

    Returns:
        One (batch, host) pair per batch, in input order.

    Raises:
        NoHeadroom: no candidate host (the pinned one, or any host, per `pin`) has room
            left for a batch.
    """
    used = {r.host: 0 for r in readings}
    placed = []
    for batch in batches:
        candidates = []
        for r in readings:
            if pin and r.host != pin:
                continue
            room = headroom(r, reservation)
            if room is None:
                continue
            room -= used[r.host]
            if room >= 0:
                candidates.append((room, r.host))
        if not candidates:
            raise NoHeadroom(
                "no headroom for batch %s — %s"
                % (batch, _describe(readings, used, reservation))
            )
        _, host = max(candidates)
        used[host] += reservation
        placed.append((batch, host))
    return placed
