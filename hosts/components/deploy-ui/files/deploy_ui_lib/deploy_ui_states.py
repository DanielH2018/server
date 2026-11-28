"""Each landing's state and the turn's locks, read from the files the landings write.

A landing writes its state record, `<pr>.<uid>.state`, into the line's directory on the turn
host at each change of state (`scripts/deploy_tools/land_lib/transitions.py`). A landing on
daniel-server writes it there through ssh, and the daemon runs on the turn host, so one read of
one directory sees the landings of both hosts. The turn, serial and slot locks are files beside
that directory, read from `/proc/locks`.

The daemon cannot import land_lib, so the names below are copies, and a test pins each one to
the land_lib name it copies. A state this module has no label for still renders, by its raw
name.
"""

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from deploy_ui_lib.deploy_ui_reads import FileLock, LockKey, lock_key

LOCK_DIR = Path("/run/lock")
LINE_DIR_NAME = "homelab-land-line"
TURN_LOCK = "homelab-land-turn.lock"
SERIAL_LOCK = "homelab-land-serial.lock"
SERIAL_HOLDER = "homelab-land-serial.holder"
PR_LOCK = "homelab-land-pr-{pr}.lock"
SLOT_LOCKS = tuple(f"homelab-land-slot-{n}.lock" for n in range(6))
# A record left this long ago is not shown, whatever its state.
WINDOW_S = 24 * 3600
VERDICT = "verdict"
# A record is a few hundred bytes; a larger file is not one.
MAX_RECORD_BYTES = 64 * 1024
_RECORD_NAME = re.compile(r"\d+\.\d+\.state")

STATE_LABELS: dict[str, str] = {
    "armed": "Armed",
    "handed-off": "Handed off",
    "own-run": "Own CI run",
    "queued": "Queued",
    "holding": "Holding the turn",
    "yielded": "Yielded",
    "testing": "Testing",
    "split": "Split",
    "proving": "Proving",
    "merging": "Merging",
    "rejoining": "Rejoining",
    "no-turn": "No turn",
    "merged": "Merged",
    "resolving": "Resolving",
    "master-ci": "Master CI",
    "promoting": "Promoting",
    "host-wait": "Host wait",
    "grading": "Grading",
    "verdict": "Verdict",
}

# Where the newest of these states leaves a landing in the turn's queue. "" is out of it.
_QUEUE_OF = {
    "queued": "waiting",
    "holding": "turn",
    "yielded": "stepped-out",
    "no-turn": "",
    "rejoining": "",
    "merged": "",
}
_QUEUE_RANK = {"turn": 0, "waiting": 1, "stepped-out": 2, "": 3}


def label(state: str) -> str:
    """The page's name for `state`, or the raw name for a state this module does not know."""
    return STATE_LABELS.get(state, state)


@dataclass(frozen=True)
class Progress:
    done: int
    total: int
    what: str


@dataclass(frozen=True)
class Record:
    """One landing's state record, as `transitions.Transitions._write` writes it.

    Attributes:
        history: Each state before `state`, oldest first, with the epoch second it began.
        since: The epoch second the landing entered `state`.
    """

    pr: str
    host: str
    pid: int
    state: str
    since: float
    history: tuple[tuple[str, float], ...]
    progress: Progress | None
    verdict: str

    @classmethod
    def parse(cls, raw: dict) -> "Record":
        """The record from its parsed JSON.

        Raises:
            KeyError, TypeError, ValueError: the object is not a state record.
        """
        p = raw["progress"]
        return cls(
            pr=str(raw["pr"]),
            host=str(raw["host"]),
            pid=int(raw["pid"]),
            state=str(raw["state"]),
            since=float(raw["since"]),
            history=tuple((str(h["state"]), float(h["since"])) for h in raw["history"]),
            progress=Progress(int(p["done"]), int(p["total"]), str(p["what"]))
            if p
            else None,
            verdict=str(raw.get("verdict") or ""),
        )

    def queue(self) -> tuple[str, float]:
        """Where the landing stands in the turn's queue, and since when.

        The newest turn state the landing passed through decides it: `turn` after `holding`,
        `waiting` after `queued`, `stepped-out` after `yielded`, and "" once it left the turn
        or never asked for it.
        """
        for state, since in reversed((*self.history, (self.state, self.since))):
            if state in _QUEUE_OF:
                return _QUEUE_OF[state], since
        return "", self.since


@dataclass(frozen=True)
class Records:
    """What one read of the records directory found.

    Attributes:
        records: Each PR's newest readable record that has no verdict and is in the window.
        unavailable: Why the directory or some records could not be read; "" when all could.
    """

    records: list[Record]
    unavailable: str


def read_records(directory: Path, now: float) -> Records:
    """Each PR's newest record in `directory`, and what could not be read.

    A PR landed by two users has a record from each, and the newest `since` is the live one.
    A file that is not a record is skipped, so one bad file never blanks the panel. A file
    that cannot be read is counted in `unavailable`, because a landing whose record this user
    cannot read would otherwise vanish from the queue. A missing directory is no landing.
    """
    try:
        names = [p for p in directory.iterdir() if _RECORD_NAME.fullmatch(p.name)]
    except FileNotFoundError:
        return Records([], "")
    except OSError as exc:
        return Records([], f"{directory.name}: {exc}")
    newest: dict[str, Record] = {}
    unreadable = 0
    for path in names:
        try:
            raw = _read(path)
        except PermissionError:
            unreadable += 1
            continue
        except OSError:
            continue
        try:
            record = Record.parse(json.loads(raw.decode("utf-8")))
        except KeyError, TypeError, ValueError, AttributeError:
            continue
        if now - record.since > WINDOW_S:
            continue
        if record.pr not in newest or record.since > newest[record.pr].since:
            newest[record.pr] = record
    return Records(
        [r for r in newest.values() if r.state != VERDICT],
        f"{unreadable} state record(s) in {directory.name} cannot be read"
        if unreadable
        else "",
    )


def _read(path: Path) -> bytes:
    """The first bytes of the regular file at `path`, never following a link or blocking.

    Any user can create a file in the line's sticky directory, so a link, a FIFO or a device
    at a record's name is refused rather than read.

    Raises:
        PermissionError: this user cannot read the file.
        OSError: the file is a link, is not a regular file, is too large, or is gone.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{path.name} is not a regular file")
        data = os.read(fd, MAX_RECORD_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > MAX_RECORD_BYTES:
        raise OSError(f"{path.name} is larger than a state record")
    return data


def _held(lock_dir: Path, name: str, table: dict[LockKey, FileLock]) -> bool:
    """Whether a process holds the lock file `name` in `lock_dir`; a missing file is free."""
    try:
        lock = table.get(lock_key(str(lock_dir / name)))
    except OSError:
        return False
    return bool(lock and lock.held)


def landing_rows(
    records: list[Record],
    now: float,
    hostname: str,
    lock_dir: Path,
    table: dict[LockKey, FileLock],
    proc: Path = Path("/proc"),
) -> list[dict]:
    """One row per landing: the turn's holder, the waiting in order, then the rest.

    The waiting landings rank by the time each entered `queued`, and `position` numbers them
    from 1. The turn goes to whichever landing polls first (`queue_machine.may_take`), so the
    rank is the order they began waiting, not a promise of the order the turn serves them.

    A landing that died is `alive: false` and sorts last, out of the queue. A record's pid
    belongs to the host that wrote it, so a landing on this host is alive while `proc` holds
    its pid, and one on another host while its PR lock in `lock_dir` is held: that landing
    holds the lock through an ssh session the turn host frees once its heartbeat stops
    (`pr_lock.py`).
    """
    placed = []
    for r in records:
        if r.host.split(".")[0] == hostname.split(".")[0]:
            alive = (proc / str(r.pid)).exists()
        else:
            alive = _held(lock_dir, PR_LOCK.format(pr=r.pr), table)
        where, queued_at = r.queue() if alive else ("", r.since)
        placed.append(((not alive, _QUEUE_RANK[where], queued_at), r, alive, where))
    placed.sort(key=lambda p: p[0])
    rows, waiting = [], 0
    for _, r, alive, where in placed:
        waiting += where == "waiting"
        rows.append(
            {
                "pr": r.pr,
                "host": r.host,
                "pid": r.pid,
                "state": r.state,
                "label": label(r.state),
                "since": r.since,
                "in_state_s": max(0, int(now - r.since)),
                "history": [
                    {"state": s, "label": label(s), "since": t} for s, t in r.history
                ],
                "progress": r.progress.__dict__ if r.progress else None,
                "alive": alive,
                "queue": where,
                "position": waiting if where == "waiting" else None,
            }
        )
    return rows


def turn_locks(lock_dir: Path, table: dict[LockKey, FileLock]) -> dict:
    """Whether the turn and serial locks are held, the serial holder's PR, the slots in use.

    The serial holder file outlives the landing that wrote it, so its number counts only while
    that PR's own landing lock is held, as `serial_lock.holder` reads it. A lock file that does
    not exist is free.
    """

    def held(name: str) -> bool:
        return _held(lock_dir, name, table)

    serial_pr = ""
    if held(SERIAL_LOCK):
        try:
            number = (lock_dir / SERIAL_HOLDER).read_text().strip()
        except OSError:
            number = ""
        if number.isdigit() and held(PR_LOCK.format(pr=number)):
            serial_pr = number
    return {
        "turn_held": held(TURN_LOCK),
        "serial": {"held": held(SERIAL_LOCK), "pr": serial_pr},
        "slots": {"used": sum(held(n) for n in SLOT_LOCKS), "of": len(SLOT_LOCKS)},
    }
