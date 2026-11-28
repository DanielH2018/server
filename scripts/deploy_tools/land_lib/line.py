"""The line for the landing turn: the landings that wait for it, each in the order it asked.

Every landing that waits for the turn holds a place: one file per PR on `turn.TURN_HOST`. The
place holds the landing's ticket, the time on the turn host's clock when the landing first
asked for the turn. A landing writes the same ticket back each time it stands again, after a
step out of the turn too, so it keeps its order. As it stands it reads the other fresh
tickets, and `queue_machine.may_take` defers it to an older one. A place that a landing on
older code wrote holds no ticket and reads as older than every ticket.

A landing that waits with a draft that has had no CI run also names the head it would test.
The landing that holds the turn can then test those heads with its own, stacked in one CI run,
and merge them (`docs/ci-throughput.md`, slice B3). It writes what became of a PR into that
PR's file, and the landing that stands there reads it on its next poll. A place with no draft
names `NO_HEAD`, and no holder stacks it.

A place counts while its landing keeps it fresh, once every merge poll. A landing that dies
stops, so its place goes stale, nobody acts on it, and the next read of the line removes it.

A landing stacks and answers only the places its own user wrote, where the user is the one on
the turn's host: for a landing on another host, the user its ssh logs in as. It reads every
user's tickets, because both users' landings take the same turn. Every place is written by
renaming a file over it, and the directory is sticky, as /tmp is, so no user but the one that
created the directory can replace another's place. A place some other user left for the same
PR therefore closes the line to that landing.

The line is not a safety gate. A landing that cannot use it waits for the turn without it.

The directory also holds each landing's state record (`transitions.py`), `<pr>.<uid>.state`,
written through the same operations and the same ssh session as a place. The uid in the name
keeps a landing from ever replacing another user's record, which the sticky bit refuses.
"""

import inspect
import os
import shlex
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from deploy_tools.land_lib.core import queue_state
from deploy_tools.land_lib.serial_lock import TURN_DIR_ENV, TURN_HOST, is_turn_host

# Beside the turn's lock file, on the same tmpfs.
DIR = Path("/run/lock/homelab-land-line")
# A landing that polls slower than four merge polls at 30 seconds reads as gone and lands alone.
FRESH_S = 120
# What the holder of the turn writes into a place once it has merged that PR.
MERGED = "merged"
ALONE = "alone"
SPLIT = "split"
# The holder found the PR's head conflicts with master, so its landing ends `merge-conflict`.
CONFLICT = "conflict"
# The head a place names when its landing has no draft to stack.
NO_HEAD = "-"
_SSH_TIMEOUT_S = 30

# One operation on the line, by name: its arguments as strings, its result as text.
type Line = Callable[..., str]


def _op(directory: str, op: str, *args: str) -> str:
    """Run one operation on the line kept in `directory`.

    It names nothing but `os`, `time` and `FRESH_S`, and uses no syntax newer than Python
    3.10, because `over_ssh` runs this source under the turn host's own `python3`.

    A place holds its head, then `@<ticket>` when its landing has a ticket, then the answer,
    if any.

    - `join <pr> <head> <ticket>` writes the PR's place anew, with no answer. An empty ticket
      is replaced by the turn host's clock, in nanoseconds.
    - `stand <pr> <head> <ticket>` refreshes the place. A place that is missing, or names
      another head or ticket, is written anew.
    - Both return `<ticket> <answer>` on the first line, then a `<pr> <ticket>` line for each
      other PR's place, of any user, that is fresh within `FRESH_S` and whose landing still
      waits. A place with no ticket reads as ticket 0.
    - `leave <pr>` removes the place.
    - `waiting <fresh_s>` returns a `<pr> <head> <ticket>` line for each draft's place of this
      user that is fresh and has no answer, and removes this user's stale places.
    - `answer <pr> <head> <what>` writes `what` into the place, if it still names `head`
      and holds no answer yet or `split`. An empty `what` leaves the place unanswered.
    - `record <pr> <text>` replaces this user's state record for the PR, `<pr>.<uid>.state`,
      with `text`.

    Raises:
        ValueError: The PR is not a number, or the operation has no such name.
        OSError: The directory or a place cannot be written.
    """
    if op not in ("join", "stand", "leave", "waiting", "answer", "record"):
        raise ValueError(f"no such operation on the line: {op}")
    if op != "waiting" and not args[0].isdigit():
        raise ValueError(f"not a pull request number: {args[0]}")
    try:
        os.mkdir(directory)
        os.chmod(directory, 0o1777)  # a umask would otherwise keep other users out
    except FileExistsError:
        pass

    def words(name: str, anyone: bool = False) -> list[str]:
        """The words of the place `name`; none when it is not this user's, unless `anyone`."""
        path = f"{directory}/{name}"
        try:
            if not anyone and os.lstat(path).st_uid != os.getuid():
                return []
            with open(path, encoding="utf-8", errors="replace") as handle:
                return handle.read().split()
        except OSError:
            return []

    def parse(place: list[str]) -> tuple[str, str, str]:
        """A place's head, ticket and answer, each "" when the place holds none."""
        tickets = [w[1:] for w in place[1:] if w.startswith("@")]
        answers = [w for w in place[1:] if not w.startswith("@")]
        return (place or [""])[0], (tickets or [""])[0], (answers or [""])[0]

    def tickets(pr: str, ticket: str, answer: str) -> str:
        rows = [f"{ticket} {answer}".rstrip()]
        oldest = time.time() - FRESH_S
        for name in sorted(os.listdir(directory)):
            if not name.isdigit() or name == pr:
                continue
            try:
                if os.lstat(f"{directory}/{name}").st_mtime < oldest:
                    continue
            except OSError:
                continue
            place = words(name, anyone=True)
            _, theirs, said = parse(place)
            if place and said not in ("merged", "conflict"):
                rows.append(f"{name} {theirs or 0}")
        return "\n".join(rows)

    def replace(name: str, text: str) -> None:
        # A reader never finds a half-written file, and a link at its name is replaced, not
        # followed.
        scratch = f"{directory}/.{name}.{os.getpid()}"
        with open(scratch, "x", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(scratch, f"{directory}/{name}")

    def write(pr: str, *place: str) -> None:
        replace(pr, " ".join(place) + "\n")

    if op == "record":
        replace(f"{args[0]}.{os.getuid()}.state", args[1])
        return ""
    if op in ("join", "stand"):
        pr, head, ticket = args
        ticket = ticket or str(time.time_ns())
        held, kept, answer = parse(words(pr))
        if op == "join" or (held, kept) != (head, ticket):
            write(pr, head, f"@{ticket}")
            answer = ""
        else:
            os.utime(f"{directory}/{pr}")
        return tickets(pr, ticket, answer)
    if op == "leave":
        if words(args[0]):
            os.unlink(f"{directory}/{args[0]}")
        return ""
    if op == "waiting":
        oldest = time.time() - float(args[0])
        found = []
        for name in sorted(os.listdir(directory)):
            try:
                status = os.lstat(f"{directory}/{name}")
                if not name.isdigit() or status.st_uid != os.getuid():
                    continue
                if status.st_mtime < oldest:
                    os.unlink(f"{directory}/{name}")
                    continue
            except OSError:
                # A landing left the line between the listing and this read.
                continue
            head, ticket, answer = parse(words(name))
            if head not in ("", "-") and not answer:
                found.append(f"{name} {head} {ticket or 0}")
        return "\n".join(found)
    pr, head, what = args
    held, ticket, answer = parse(words(pr))
    if held == head and answer in ("", "split"):
        write(pr, head, *([f"@{ticket}"] if ticket else []), *([what] if what else []))
    return ""


def here(directory: Path = DIR) -> Line:
    """The line on this host."""
    return lambda op, *args: _op(str(directory), op, *args)


def at_turn_host(hostname: str, directory: Path = DIR) -> Line:
    """The line for the turn, as the host named `hostname` reaches it.

    `TURN_DIR_ENV` moves it into a directory on this host, whatever the host.
    """
    moved = os.environ.get(TURN_DIR_ENV)
    if moved:
        return here(Path(moved) / directory.name)
    if is_turn_host(hostname):
        return here(directory)
    return over_ssh(TURN_HOST, directory)


_REMOTE = f"""import os, sys, time
FRESH_S = {FRESH_S}
{inspect.getsource(_op)}
print(_op(*sys.argv[1:]))
"""


def over_ssh(host: str, directory: Path = DIR) -> Line:
    """The line on `host`, one ssh command for each operation.

    Raises:
        OSError: `host` did not answer, or the operation failed there.
    """

    def run(op: str, *args: str) -> str:
        words = ("python3", "-c", _REMOTE, str(directory), op, *args)
        try:
            done = subprocess.run(
                [
                    "ssh",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "ConnectTimeout=10",
                    host,
                    " ".join(shlex.quote(word) for word in words),
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=_SSH_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired as exc:
            raise OSError(f"ssh {host}: no answer in {_SSH_TIMEOUT_S}s") from exc
        if done.returncode != 0:
            why = done.stderr.strip().splitlines()
            raise OSError(f"ssh {host}: {why[-1] if why else 'no answer'}")
        return done.stdout.strip()

    return run


class Place:
    """One landing's place in the line, and the ticket it keeps for as long as it lives.

    The first stand takes a ticket from the turn host's clock. Every later one, after a leave
    too, writes the same ticket back. A landing whose line has failed has a place that does
    nothing and reads no other ticket.

    Attributes:
        ticket: The ticket the turn host gave this landing, or 0 before its first stand.
    """

    def __init__(self, line: Line | None, pr: str, say: Callable[[str], None]) -> None:
        self._line, self._pr, self._say = line, pr, say
        self.ticket = 0
        self._head: str | None = None

    def _run(self, op: str, *args: str) -> str:
        if self._line is None:
            return ""
        try:
            return self._line(op, *args)
        except (OSError, ValueError) as exc:
            self._say(
                f"could not use the line for the landing turn ({exc}); waiting for the "
                "turn without it"
            )
            self._line = None
            return ""

    def stand(self, head: str) -> tuple[str, tuple[queue_state.Place, ...]]:
        """Take the place with `head`, or refresh it, and read the line.

        A stand with a head other than the last one's starts the place anew, so an answer
        left for an earlier landing of the same head, or for the old head, is never read as
        this one's.

        Args:
            head: The draft's head a holder may stack, or "" for a landing with none.

        Returns:
            What the holder of the turn answered, and the other PRs' fresh places, each with
            its ticket.
        """
        op = "stand" if self._head == head else "join"
        self._head = head
        reply = self._run(op, self._pr, head or NO_HEAD, str(self.ticket or ""))
        if not reply:
            return "", ()
        first, *rows = (row.split() for row in reply.splitlines())
        self.ticket = int(first[0])
        others = tuple(queue_state.Place(pr, ticket=int(t)) for pr, t in rows)
        return (first[1] if len(first) > 1 else ""), others

    def answer(self) -> str:
        """What the holder of the turn answered this landing's place; "" when it has none."""
        return self.stand(self._head)[0] if self._head is not None else ""

    def leave(self) -> None:
        """Give the place up, keeping the ticket for the next stand."""
        if self._head is not None:
            self._head = None
            self._run("leave", self._pr)
