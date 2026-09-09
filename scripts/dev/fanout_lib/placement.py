"""Pure placement over (host, cap, current, live_agents) — spec 2026-09-06 §2.

Decides; does not fetch. The one read it needs is READ_COMMAND, run by transport.read_host.
"""

from collections.abc import Sequence
from dataclasses import dataclass

# DECIDED: 2.5 GiB per agent. daniel-box's 24h peak for user-1000.slice was 7.2 GiB
# (claude_code defaults/main.yml derivation) with three to four `claude` processes live, so
# ~2 GiB each at peak; 2.5 GiB adds a pytest fan-out's worth (4 workers x ~150 MB). Confirm
# against claude_cgroup_memory_current_bytes / claude_cgroup_pids_current before raising.
RESERVATION_BYTES = 2_684_354_560

# One line each: user.slice memory.current, its memory.high (an integer, or `max` when no
# drop-in caps it), and the count of live `claude` processes for uid 1000. `pgrep -c` exits 1
# on a zero count, which is why it is the last command and not the exit status we read.
READ_COMMAND = (
    "cat /sys/fs/cgroup/user.slice/memory.current /sys/fs/cgroup/user.slice/memory.high; "
    "pgrep -c -x claude -u 1000"
)


@dataclass(frozen=True)
class HostReading:
    host: str
    cap_bytes: int | None
    current_bytes: int
    live_agents: int


class NoHeadroom(Exception):
    """Neither host can take the batch; str() carries every host's numbers."""


def parse_reading(host: str, stdout: str) -> HostReading:
    """Parse READ_COMMAND's three-line output into a HostReading.

    Args:
        host: the host the reading came from.
        stdout: the command's stdout — memory.current, memory.high (or `max`), then the
            live-agent count, one per line.

    Returns:
        The parsed reading.

    Raises:
        ValueError: the output is not exactly three non-blank lines, or a numeric field
            does not parse as an integer.
    """
    lines = [ln.strip() for ln in stdout.splitlines() if ln.strip()]
    if len(lines) != 3:
        raise ValueError(
            "%s: expected 3 lines from the headroom read, got %r" % (host, stdout)
        )
    current, high, agents = lines
    cap = None if high == "max" else int(high)
    return HostReading(host, cap, int(current), int(agents))


def headroom(reading: HostReading, reservation: int = RESERVATION_BYTES) -> int | None:
    """Bytes free under the cap after one reservation, or None when the host is uncapped."""
    if reading.cap_bytes is None:
        return None
    return reading.cap_bytes - reading.current_bytes - reservation


def _describe(
    readings: Sequence[HostReading], used: dict[str, int], reservation: int
) -> str:
    parts = []
    for r in readings:
        room = headroom(r, reservation)
        shown = (
            "uncapped"
            if room is None
            else "%.1f GiB free" % ((room - used[r.host]) / 1024**3)
        )
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
