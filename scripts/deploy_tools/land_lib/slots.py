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
dies frees its slot, and a slot held over ssh ends when its host stops writing the heartbeat
(`turn.HEARTBEAT_S`).

The count drops to 1 while the core quota runs low (`live_limit.low_quota`). When the turn host
does not answer, a landing takes one of `FALLBACK_COUNT` slots on its own host instead, so each
host is bounded on its own until the turn host answers again.

The slots are not a safety gate. A landing waits for one only as long as its two merge budgets
leave, which is what `LAND_DEADLINE` counts before the master CI wait
(`hosts/tests/repo_crons/test_lander_deadlines_cover_the_soak.py`), then goes on without one. A landing
that cannot open the lock files goes on without one at once.

`turn.TURN_DIR_ENV` moves the slots with the turn into a directory on this host, so a test that
runs a whole landing never takes a real slot.
"""

import contextlib
import os
from collections.abc import Callable, Generator, Sequence
from dataclasses import dataclass
from enum import Enum
from functools import partial
from pathlib import Path
from typing import NoReturn


from deploy_tools.land_lib import live_limit, pin_gate, turn
from deploy_tools.land_lib.landing import BRANCH, Landing
from deploy_tools.land_lib.outcome import Verdict, say
from deploy_tools.land_lib.serial_lock import SERIAL_NAME, record_holder
from lib.path_kinds import moves_the_bundle
from lib.progress import OnChange

# DECIDED: 6. The operator set it on 2026-11-15 (D10 in docs/ci-throughput.md): on 2026-11-14,
# 6 to 7 live landings spent about 1,700 of the 5,000 core calls an hour and 2,250 of the 5,000
# GraphQL calls. Lower it from data.
COUNT = 6
# Per-host limit `live_limit` set before the slots existed.
FALLBACK_COUNT = 3
_LOCK_DIR = turn.LOCK.parent
SLOT_NAMES = tuple(f"homelab-land-slot-{n}.lock" for n in range(COUNT))
# DECIDED: 2400 s, the longest a serial PR in the turn waits for the landings past their merge
# (`_wait_out`). On 2026-11-18 and 2026-11-19 every landing that settled took 7 to 2209 s from its
# merge to its verdict (the `landing-annotation` records' wait_ci plus deploy). A landing stuck
# on a soak for longer would otherwise hold the turn for two merge budgets.
DRAIN_S = 2400
# DECIDED: 300 s, how often a landing waiting for the serial landing reads whether its PR
# conflicts. One REST read each: 15 waiting landings spend 180 core calls an hour, where a read
# every poll would spend 1,800.
CONFLICT_READ_S = 300
_SERIAL_LIVE = f"landing of a PR that changes {live_limit.SERIAL_WHAT} is live"
# The kinds of PR that change a k8s role, which a hold and the no-promotion window bar.
_RENDERS = (live_limit.K8S, live_limit.SERIAL)

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


def _keep_serial(ln: Landing, lock: Locker) -> bool:
    """Take the serial lock for the rest of the landing; whether another landing holds it."""
    if ln.serial_lock:
        return False
    try:
        ln.serial_lock = lock([SERIAL_NAME], 1)
    except OSError:
        return False
    if ln.serial_lock:
        # Only `probe.py landing` and `cc-wait landing-clear` read the name.
        with contextlib.suppress(OSError):
            record_holder(ln.tools.hostname(), ln.opts.pr)
    return ln.serial_lock is None


class Clears(Enum):
    """What clears a bar `_bar` names, which decides how a landing waits for it."""

    # Nothing a wait can see: the landing is refused at once.
    NEVER = "never"
    # Landings past their merge wait for their verdicts holding the turn, so no other landing merges
    # meanwhile.
    VERDICTS = "verdicts"
    # A landing waits for the serial landing's process end with the turn given back, because the
    # serial landing may need the turn before its merge.
    SERIAL_ENDS = "serial-ends"


@dataclass(frozen=True)
class Bar:
    """A rule that keeps a PR from merging now: the reason, and what clears it."""

    why: str
    clears: Clears


_WAITING = {
    Clears.VERDICTS: (
        "waiting in the landing turn for the landings past their merge to reach their "
        "verdicts"
    ),
    Clears.SERIAL_ENDS: "waiting in line for the serial landing to end",
}
_CLEARED = {
    Clears.VERDICTS: "the landings past their merge reached their verdicts",
    Clears.SERIAL_ENDS: "the serial landing ended",
}


def _bar(ln: Landing, kind: str, lock: Locker) -> Bar | None:
    """Why `ln`'s PR, of `kind`, may not merge now, and what clears that.

    Returns:
        The bar, or None when nothing bars the PR. Inside the no-promotion window no landing
        reaches its verdict, so no bar clears there.
    """
    if kind in _RENDERS:
        hold = live_limit.fail_open(
            lambda: pin_gate.hold_problem(ln.tools.flux.pins.hold())
        )
        if hold:
            return Bar(f"{hold}, and this PR changes a k8s role", Clears.NEVER)
    in_window = bool(
        kind in _RENDERS and live_limit.fail_open(ln.tools.in_reboot_window)
    )
    if kind == live_limit.SERIAL:
        if _keep_serial(ln, lock):
            return Bar(
                f"another {_SERIAL_LIVE}", _unless(in_window, Clears.SERIAL_ENDS)
            )
        if _held_by_another(lock, SLOT_NAMES):
            return Bar(
                f"this PR changes {live_limit.SERIAL_WHAT}, and another landing is past "
                "its merge",
                _unless(in_window, Clears.VERDICTS),
            )
    elif kind != live_limit.QUIET and _held_by_another(lock, [SERIAL_NAME]):
        return Bar(f"a {_SERIAL_LIVE}", _unless(in_window, Clears.SERIAL_ENDS))
    if in_window and _held_by_another(lock, SLOT_NAMES):
        return Bar(
            "inside the weekly reboot's no-promotion window, another landing is past its "
            "merge, and this PR changes a k8s role",
            Clears.NEVER,
        )
    return None


def _unless(in_window: bool, clears: Clears) -> Clears:
    return Clears.NEVER if in_window else clears


def _conflict_reads(ln: Landing) -> Callable[[int], None]:
    """The call a wait for the serial landing makes each poll, with the seconds it has waited.

    It reads GitHub's `mergeable` for `ln`'s PR at once and every `CONFLICT_READ_S` after,
    and again on the next poll after a CONFLICTING read, because master moving flips the field
    for one read. Two CONFLICTING reads in a row end the landing `merge-conflict`. A read that
    fails reads as no conflict.
    """
    due, conflicting = 0, False

    def read(waited: int) -> None:
        nonlocal due, conflicting
        if waited < due and not conflicting:
            return
        view = live_limit.fail_open(lambda: ln.view("mergeable"))
        now = bool(view) and view.get("mergeable") == "CONFLICTING"
        if now and conflicting:
            ln.die(
                f"PR #{ln.opts.pr} conflicts with {BRANCH}, read while it waited in line for "
                "the serial landing to end — rebase it, push, then re-run this",
                1,
                Verdict.MERGE_CONFLICT,
            )
        conflicting, due = now, waited + CONFLICT_READ_S

    return read


def _wait_out(ln: Landing, kind: str, lock: Locker, bar: Bar) -> Bar | None:
    """Wait while the bar on `ln`'s PR stays one that `bar.clears` clears.

    The wait ends when the merge budgets run out. A wait in the turn ends after `DRAIN_S` at
    the latest, because no other landing merges while it lasts. A wait for the serial landing
    gives the turn back, so it holds nothing another landing needs, and only the budgets
    bound it: the serial lock is held from the serial draft's turn, through its CI run, its
    merge and its deploy, which is longer than `DRAIN_S` measures. No holder of the turn
    reads such a landing's head, so that wait reads its own PR's mergeability
    (`_conflict_reads`). Each poll of a wait for the serial landing adds its seconds to
    `ln.ledger.serial_wait` as it passes, so a landing that ends during the wait records it.

    Returns:
        The bar the wait ended on: None once nothing bars the PR, a bar of another kind as
        soon as one applies, and `Clears.NEVER` with the reason when time ran out.

    Raises:
        Outcome: `merge-conflict`, when the PR conflicts with master during a wait for the
            serial landing.
    """
    o = ln.opts
    budget = _budget_left(ln)
    if bar.clears is Clears.VERDICTS:
        budget = min(DRAIN_S, budget)
    serial = bar.clears is Clears.SERIAL_ENDS
    conflicts = _conflict_reads(ln) if serial else None
    waiting = OnChange(say)
    waited = 0
    while waited + o.merge_poll <= budget:
        if conflicts:
            conflicts(waited)
        waiting(f"{_WAITING[bar.clears]}: {bar.why}")
        ln.tools.sleep(o.merge_poll)
        waited += o.merge_poll
        if serial:
            ln.ledger.serial_wait += o.merge_poll
        now = _bar(ln, kind, lock)
        if now is None:
            say(f"{_CLEARED[bar.clears]} after {waited}s")
            return None
        if now.clears is not bar.clears:
            return now
        bar = now
    return Bar(f"{bar.why}, after {waited}s of {_WAITING[bar.clears]}", Clears.NEVER)


def _bar_after_waits(ln: Landing, head: str, in_turn_waits: bool) -> Bar | None:
    """The bar on `ln`'s PR at `head` once every wait that can clear it has run.

    A live serial landing is waited out with the turn given back, and asked for again before
    the bar is read once more (`Landing.without_turn`). With `in_turn_waits`, a landing that
    holds the turn waits there for the landings past their merge to reach their verdicts.

    Returns:
        The bar the PR still has, or None when nothing bars it.
    """
    kind, lock = _kind(ln, head), _lock_of(ln)
    bar = _bar(ln, kind, lock)
    while bar is not None:
        if bar.clears is Clears.SERIAL_ENDS:
            bar = ln.without_turn(
                partial(_wait_out, ln, kind, lock, bar),
                "to wait in line for the serial landing to end",
            ) or _bar(ln, kind, lock)
        elif bar.clears is Clears.VERDICTS and in_turn_waits and ln.give_back_turn:
            bar = _wait_out(ln, kind, lock, bar)
        else:
            break
    return bar


def _kind(ln: Landing, head: str) -> str:
    """The kind of the PR's files at `head`; "" when they cannot be read.

    `live_limit.QUIET` for a PR that moves no bundle (`lib.path_kinds.moves_the_bundle`), else
    `live_limit.kind`.
    """
    paths = live_limit.fail_open(lambda: ln.changed_paths(head))
    if not paths:
        return ""
    if not moves_the_bundle(paths):
        return live_limit.QUIET
    return live_limit.kind(paths)


def _lock_of(ln: Landing) -> Locker:
    return ln.tools.try_slot or _locker(ln.tools.hostname())


def bar_member(member: Landing, head: str) -> str:
    """Why the PR `member` lands may not join a batch now; "" when nothing bars it.

    The holder merges its members before any of them holds a slot, so inside the weekly
    reboot's no-promotion window a k8s PR joins no batch: it waits out the window alone.
    """
    kind = _kind(member, head)
    if kind in _RENDERS and live_limit.fail_open(member.tools.in_reboot_window):
        return "it changes a k8s role inside the weekly reboot's no-promotion window"
    bar = _bar(member, kind, _lock_of(member))
    return bar.why if bar else ""


def _refuse(ln: Landing, why: str) -> NoReturn:
    ln.die(
        f"{why}; nothing is merged. Re-run the same command once that clears",
        75,
        Verdict.REFUSED,
    )


def refuse_before_run(ln: Landing, head: str) -> None:
    """End the landing of a draft that takes the turn while a rule bars it, before its run.

    A live serial landing is waited out first, so the draft spends no run beside it. A bar
    that the landings past their merge clear (`_bar`) lets the run start: the turn keeps
    other merges out during the run, and `refuse_before_merge` waits out the rest.
    """
    bar = _bar_after_waits(ln, head, in_turn_waits=False)
    if bar and bar.clears is not Clears.VERDICTS:
        _refuse(ln, bar.why)
    if bar:
        say(f"{bar.why}; starting its run, and it waits in the turn for that to clear")


def refuse_before_merge(ln: Landing, head: str) -> None:
    """End the landing with exit 75, merging nothing, while a rule bars its PR from merging.

    It runs just before the merge, and `refuse_before_run` runs the same rules when a draft
    takes the turn. The rules, for a PR of `live_limit.kind`:

    - a k8s or serial PR waits while `DEPLOY_HOLD` holds;
    - every PR but a `live_limit.QUIET` one waits while another landing holds the serial
      lock. Outside the no-promotion window it waits in line, with the landing turn given
      back, for that landing to end, and refuses only once its merge budgets run out. A PR
      that conflicts with master during that wait ends `merge-conflict` there. A
      quiet PR waits on no promotion, so the serial landing's cannot fail it;
    - a serial PR waits while another landing holds a slot, and keeps the serial lock until
      its process ends, a fresh grading included, so every other PR waits for it. While it
      holds the landing turn (`ln.give_back_turn`), it waits up to `DRAIN_S` for those
      landings to reach their verdicts before it refuses, because no other landing merges
      meanwhile;
    - a k8s or serial PR waits inside the weekly reboot's no-promotion window while another
      landing holds a slot, so one at a time waits out the window.

    Every read fails open: a rule whose read fails bars nothing.
    """
    bar = _bar_after_waits(ln, head, in_turn_waits=True)
    if bar:
        _refuse(ln, bar.why)
