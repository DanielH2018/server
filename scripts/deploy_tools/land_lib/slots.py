"""The landing slots: how many landings may poll GitHub between their merge and their verdict.

Every landing polls GitHub on one shared token from its merge to its verdict: master CI, the
promotion, the hosts and the health gate. The slots bound how many do that at once, across
both hosts. Before the merge the landing turn (`turn.py`) is the bound: only the landing that
holds it polls its PR's CI. So a landing takes a slot after its merge and its step 1, and holds
it to its verdict. A landing whose step 1 ends it, such as a docs-only PR or one whose grading
moves to a fresh checkout, takes none.

A slot is one of `COUNT` lock files on `turn.TURN_HOST`, locked the way the turn is: a landing
there locks a file itself, and a landing on another host locks one through an ssh session that
lasts as long as it holds the slot. A landing takes a free file only while other landings hold
fewer than the cap of them (`turn.lock_below`), so a cap below `COUNT` counts every holder. The lock ends with the landing's process, so a landing that
dies frees its slot.

The count drops to 1 while the core quota runs low (`live_limit.low_quota`). When the turn host
does not answer, a landing takes one of `FALLBACK_COUNT` slots on its own host instead, so each
host is bounded on its own until the turn host answers again.

The slots are not a safety gate. A landing waits for one only as long as its two merge budgets
leave, which is what `LAND_DEADLINE` counts before the master CI wait
(`hosts/tests/test_lander_deadlines_cover_the_soak.py`), then goes on without one. A landing
that cannot open the lock files goes on without one at once.

`turn.TURN_DIR_ENV` moves the slots with the turn into a directory on this host, so a test that
runs a whole landing never takes a real slot.
"""

import contextlib
import os
from collections.abc import Callable, Generator
from pathlib import Path

import sys as _sys

_sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # scripts/
from deploy_tools.land_lib import live_limit, turn
from deploy_tools.land_lib.landing import Landing
from deploy_tools.land_lib.outcome import say
from lib.progress import OnChange

# DECIDED: 6. The operator set it on 2026-11-15 (D10 in docs/ci-throughput.md): on 2026-11-14,
# 6 to 7 live landings spent about 1,700 of the 5,000 core calls an hour and 2,250 of the 5,000
# GraphQL calls. Lower it from data.
COUNT = 6
# Each host's own count while the turn host does not answer: the per-host limit `live_limit`
# set before the slots existed.
FALLBACK_COUNT = 3
_LOCK_DIR = turn.LOCK.parent

Release = Callable[[], None]


def _paths(directory: Path, count: int = COUNT) -> list[Path]:
    return [directory / f"homelab-land-slot-{n}.lock" for n in range(count)]


def try_take(paths: list[Path], cap: int) -> Release | None:
    """Take a slot among `paths` on this host without waiting, while fewer than `cap` are held.

    Returns:
        The call that frees the slot, or None while other landings hold `cap` of them.

    Raises:
        OSError: A lock file can be neither opened nor created.
    """
    fd = turn.lock_below([str(p) for p in paths], cap)
    return None if fd is None else lambda: os.close(fd)


def _taker(
    hostname: str, directory: Path = _LOCK_DIR
) -> Callable[[int], Release | None]:
    """The call that takes a slot in `directory` under a cap, from the host named `hostname`."""
    moved = os.environ.get(turn.TURN_DIR_ENV)
    if moved:
        return lambda cap: try_take(_paths(Path(moved)), cap)
    if turn.is_turn_host(hostname):
        return lambda cap: try_take(_paths(directory), cap)
    unreachable = OnChange(say)

    def take(cap: int) -> Release | None:
        try:
            return turn.try_take_over_ssh(_paths(directory), cap)
        except OSError as exc:
            unreachable(
                f"{turn.TURN_HOST} did not answer for the landing slots ({exc}); taking one "
                f"of {FALLBACK_COUNT} on this host"
            )
            return try_take(_paths(directory, FALLBACK_COUNT), cap)

    return take


def _cap(ln: Landing) -> tuple[int, str]:
    low = live_limit.low_quota(live_limit.github_reads(ln.tools.gh_json))
    return (1, low) if low else (COUNT, f"the count of {COUNT}")


def _wait(ln: Landing) -> Release | None:
    """Poll for a slot while the merge budgets last; the call that frees it, or None."""
    o = ln.opts
    spent = ln.tools.clock() - ln.ledger.t_start
    budget = max(0, int(2 * o.merge_timeout - spent))
    try_slot = ln.tools.try_slot or _taker(ln.tools.hostname())
    waiting = OnChange(say)
    waited = 0
    while True:
        cap, why = _cap(ln)
        try:
            release = try_slot(cap)
        except OSError as exc:
            say(
                f"could not open the landing slots' locks ({exc}); going on without one"
            )
            return None
        if release:
            say(f"took a landing slot after {waited}s")
            return release
        if waited + o.merge_poll > budget:
            say(
                f"every landing slot stayed held for {waited}s, and the merge budgets have "
                f"{budget - waited}s left; going on without one"
            )
            return None
        waiting(
            f"waiting for a landing slot: {cap} or more landings are between their merge and "
            f"their verdict, the limit under {why}"
        )
        ln.tools.sleep(o.merge_poll)
        waited += o.merge_poll


@contextlib.contextmanager
def held(ln: Landing) -> Generator[None]:
    """Wait for a landing slot, hold it while the block runs, and free it however it ends."""
    release = _wait(ln)
    try:
        yield
    finally:
        if release:
            release()
