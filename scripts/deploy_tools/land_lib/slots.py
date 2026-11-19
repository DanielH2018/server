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
from collections.abc import Callable, Generator, Sequence
from pathlib import Path
from typing import NoReturn

import sys as _sys

_sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # scripts/
from deploy_tools.land_lib import live_limit, pin_gate, turn
from deploy_tools.land_lib.landing import Landing
from deploy_tools.land_lib.outcome import Verdict, say
from lib.progress import OnChange

# DECIDED: 6. The operator set it on 2026-11-15 (D10 in docs/ci-throughput.md): on 2026-11-14,
# 6 to 7 live landings spent about 1,700 of the 5,000 core calls an hour and 2,250 of the 5,000
# GraphQL calls. Lower it from data.
COUNT = 6
# Each host's own count while the turn host does not answer: the per-host limit `live_limit`
# set before the slots existed.
FALLBACK_COUNT = 3
_LOCK_DIR = turn.LOCK.parent
SLOT_NAMES = tuple(f"homelab-land-slot-{n}.lock" for n in range(COUNT))
# Held by a landing of a serial PR from the check before its merge until its process ends.
SERIAL_NAME = "homelab-land-serial.lock"
# DECIDED: 2400 s, the longest a serial PR in the turn waits for the landings past their merge
# (`_drain`). On 2026-11-18 and 2026-11-19 every landing that settled took 7 to 2209 s from its
# merge to its verdict (the `landing-annotation` records' wait_ci plus deploy). A landing stuck
# on a soak for longer would otherwise hold the turn for two merge budgets.
DRAIN_S = 2400
_SERIAL_LIVE = (
    f"landing of a PR that changes {live_limit.SERIAL_WHAT} is live "
    "(`cc-wait landing-clear` waits for it)"
)

Release = Callable[[], None]
Locker = Callable[[Sequence[str], int], Release | None]


def _paths(directory: Path, count: int = COUNT) -> list[Path]:
    return [directory / name for name in SLOT_NAMES[:count]]


def try_take(paths: list[Path], cap: int) -> Release | None:
    """Take a slot among `paths` on this host without waiting, while fewer than `cap` are held.

    Returns:
        The call that frees the slot, or None while other landings hold `cap` of them.

    Raises:
        OSError: A lock file can be neither opened nor created.
    """
    fd = turn.lock_below([str(p) for p in paths], cap)
    return None if fd is None else lambda: os.close(fd)


def _locker(hostname: str, directory: Path = _LOCK_DIR) -> Locker:
    """The call that locks one of the named files in `directory` under a cap, from `hostname`.

    Off the turn host it locks over ssh. When the turn host does not answer, it locks among
    the first `FALLBACK_COUNT` of the names on this host instead.
    """
    moved = os.environ.get(turn.TURN_DIR_ENV)
    if moved or turn.is_turn_host(hostname):
        here = Path(moved) if moved else directory
        return lambda names, cap: try_take([here / n for n in names], cap)
    unreachable = OnChange(say)

    def lock(names: Sequence[str], cap: int) -> Release | None:
        try:
            return turn.try_take_over_ssh([directory / n for n in names], cap)
        except OSError as exc:
            unreachable(
                f"{turn.TURN_HOST} did not answer for the landing slots ({exc}); counting "
                f"{FALLBACK_COUNT} slots on this host"
            )
            return try_take([directory / n for n in names[:FALLBACK_COUNT]], cap)

    return lock


def _cap(ln: Landing) -> tuple[int, str]:
    low = live_limit.low_quota(live_limit.github_reads(ln.tools.gh_json))
    return (1, low) if low else (COUNT, f"the count of {COUNT}")


def _budget_left(ln: Landing) -> int:
    """The seconds the landing's two merge budgets leave, which `LAND_DEADLINE` counts."""
    spent = ln.tools.clock() - ln.ledger.t_start
    return max(0, int(2 * ln.opts.merge_timeout - spent))


def _wait(ln: Landing) -> Release | None:
    """Poll for a slot while the merge budgets last; the call that frees it, or None."""
    o = ln.opts
    budget = _budget_left(ln)
    lock = ln.tools.try_slot or _locker(ln.tools.hostname())
    waiting = OnChange(say)
    waited = 0
    while True:
        cap, why = _cap(ln)
        try:
            release = lock(SLOT_NAMES, cap)
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


def _held_by_another(lock: Locker, names: Sequence[str]) -> bool:
    """Whether another landing holds one of `names`; False when the files cannot be locked."""
    try:
        release = lock(names, 1)
    except OSError:
        return False
    if release:
        release()
    return release is None


def serial_landing_live(hostname: str) -> bool:
    """Whether a landing of a serial PR holds the serial lock, read from `hostname`.

    The read takes no lock, so it cannot make a landing's own attempt fail.

    Raises:
        OSError: The turn host did not answer; an unknown state is never "not live".
    """
    return turn.held_on_turn_host(SERIAL_NAME, hostname)


def _keep_serial(ln: Landing, lock: Locker) -> bool:
    """Take the serial lock for the rest of the landing; whether another landing holds it."""
    if ln.serial_lock:
        return False
    try:
        ln.serial_lock = lock([SERIAL_NAME], 1)
    except OSError:
        return False
    return ln.serial_lock is None


def _bar(ln: Landing, kind: str, lock: Locker) -> tuple[str, bool]:
    """Why `ln`'s PR, of `kind`, may not merge now, and whether `_drain` can clear it.

    Returns:
        The reason, "" when nothing bars the PR, and whether the bar clears once the landings
        past their merge reach their verdicts. Inside the no-promotion window they cannot,
        so no bar drains there.
    """
    if kind:
        hold = live_limit.fail_open(
            lambda: pin_gate.hold_problem(ln.tools.flux.pins.hold())
        )
        if hold:
            return f"{hold}, and this PR changes a k8s role", False
    in_window = bool(kind) and live_limit.fail_open(ln.tools.in_reboot_window)
    if kind == live_limit.SERIAL:
        if _keep_serial(ln, lock):
            return f"another {_SERIAL_LIVE}", not in_window
        if _held_by_another(lock, SLOT_NAMES):
            return (
                f"this PR changes {live_limit.SERIAL_WHAT}, and another landing is past "
                "its merge",
                not in_window,
            )
    elif _held_by_another(lock, [SERIAL_NAME]):
        return f"a {_SERIAL_LIVE}", False
    if in_window and _held_by_another(lock, SLOT_NAMES):
        return (
            "inside the weekly reboot's no-promotion window, another landing is past its "
            "merge, and this PR changes a k8s role",
            False,
        )
    return "", False


def _drain(ln: Landing, kind: str, lock: Locker, why: str) -> str:
    """Wait in the turn while the landings past their merge reach their verdicts.

    The caller holds the turn, so no other landing merges meanwhile and the bar can only
    clear. The wait ends after `DRAIN_S`, or sooner when the merge budgets run out.

    Returns:
        Why the PR still may not merge; "" once nothing bars it.
    """
    o = ln.opts
    budget = min(DRAIN_S, _budget_left(ln))
    waiting = OnChange(say)
    waited = 0
    while waited + o.merge_poll <= budget:
        waiting(
            f"waiting in the landing turn for the landings past their merge to reach their "
            f"verdicts: {why}"
        )
        ln.tools.sleep(o.merge_poll)
        waited += o.merge_poll
        why, drains = _bar(ln, kind, lock)
        if not why:
            say(f"the landings past their merge reached their verdicts after {waited}s")
            return ""
        if not drains:
            return why
    return f"{why}, after {waited}s of waiting in the landing turn"


def _kind(ln: Landing) -> str:
    return live_limit.pr_kind(ln.opts.pr, live_limit.github_reads(ln.tools.gh_json))


def _lock_of(ln: Landing) -> Locker:
    return ln.tools.try_slot or _locker(ln.tools.hostname())


def bar_member(member: Landing) -> str:
    """Why the PR `member` lands may not join a batch now; "" when nothing bars it.

    The holder merges its members before any of them holds a slot, so inside the weekly
    reboot's no-promotion window a k8s PR joins no batch: it waits out the window alone.
    """
    kind = _kind(member)
    if kind and live_limit.fail_open(member.tools.in_reboot_window):
        return "it changes a k8s role inside the weekly reboot's no-promotion window"
    return _bar(member, kind, _lock_of(member))[0]


def _refuse(ln: Landing, why: str) -> NoReturn:
    ln.die(
        f"{why}; nothing is merged. Re-run the same command once that clears",
        75,
        Verdict.REFUSED,
    )


def refuse_before_run(ln: Landing) -> None:
    """End the landing of a draft that takes the turn while a rule bars it, before its run.

    A bar that the landings past their merge clear (`_bar`) lets the run start: the turn
    keeps other merges out during the run, and `refuse_before_merge` waits out the rest.
    """
    why, drains = _bar(ln, _kind(ln), _lock_of(ln))
    if why and not drains:
        _refuse(ln, why)
    if why:
        say(f"{why}; starting its run, and it waits in the turn for that to clear")


def refuse_before_merge(ln: Landing, in_turn: bool) -> None:
    """End the landing with exit 75, merging nothing, while a rule bars its PR from merging.

    It runs just before the merge, and `refuse_before_run` runs the same rules when a draft
    takes the turn. The rules, for a PR of `live_limit.kind`:

    - a k8s or serial PR waits while `DEPLOY_HOLD` holds;
    - a serial PR waits while another landing holds a slot or the serial lock, and keeps the
      serial lock until its process ends, a fresh grading included, so every other PR waits
      for it. When `in_turn`, it waits up to `DRAIN_S` for those landings to reach their
      verdicts before it refuses, because no other landing merges meanwhile;
    - a k8s or serial PR waits inside the weekly reboot's no-promotion window while another
      landing holds a slot, so one at a time waits out the window.

    Every read fails open: a rule whose read fails bars nothing.
    """
    kind, lock = _kind(ln), _lock_of(ln)
    why, drains = _bar(ln, kind, lock)
    if why and drains and in_turn:
        why = _drain(ln, kind, lock, why)
    if why:
        _refuse(ln, why)
