"""The line for the landing turn: the draft PRs whose landings wait in it.

A landing that waits for the turn with a draft that has had no CI run stands in the line: one
file per PR on `turn.TURN_HOST`, holding the head it would test. The landing that holds the
turn can then test those heads with its own, stacked in one CI run, and merge them
(`docs/ci-throughput.md`, slice B3). It writes what became of a PR into that PR's file, and
the landing that stands there reads it on its next poll.

A place counts while its landing keeps it fresh, once every merge poll. A landing that dies
stops, so its place goes stale, nobody acts on it, and the next read of the line removes it.

A landing reads and answers only the places its own user wrote, where the user is the one on
the turn's host: for a landing on another host, the user its ssh logs in as. Every place is
written by renaming a file over it, and the directory is sticky, as /tmp is, so no user but
the one that created the directory can replace another's place. A place some other user
left for the same PR therefore closes the line to that landing.

The line is not a safety gate. A landing that cannot use it waits for the turn without it.
"""

import inspect
import os
import shlex
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

# Beside the turn's lock file, on the same tmpfs.
DIR = Path("/run/lock/homelab-land-line")
# A place older than this belongs to a landing that has stopped polling: four merge polls at
# the default of 30 seconds. A landing that polls slower reads as gone, and lands alone.
FRESH_S = 120
# What the holder of the turn writes into a place once it has merged that PR.
MERGED = "merged"
# What it writes once a batch that tested that PR was not green and the holder's own run
# was not red: the PR lands by itself.
ALONE = "alone"
_SSH_TIMEOUT_S = 30

# One operation on the line, by name: its arguments as strings, its result as text.
type Line = Callable[..., str]


def _op(directory: str, op: str, *args: str) -> str:
    """Run one operation on the line kept in `directory`.

    It names nothing but `os` and `time`, and uses no syntax newer than Python 3.10, because
    `over_ssh` runs this source under the turn host's own `python3`.

    - `join <pr> <head>` writes the PR's place anew, with no answer.
    - `stand <pr> <head>` refreshes the place and returns the answer in it. A place that is
      missing, or names another head, is written anew.
    - `leave <pr>` removes the place.
    - `waiting <fresh_s>` returns a `<pr> <head>` line for each place of this user that is
      fresh and has no answer, and removes this user's stale places.
    - `answer <pr> <head> <what>` writes `what` into the place, if it still names `head`
      and holds no answer yet.

    Raises:
        ValueError: The PR is not a number, or the operation has no such name.
        OSError: The directory or a place cannot be written.
    """
    if op not in ("join", "stand", "leave", "waiting", "answer"):
        raise ValueError(f"no such operation on the line: {op}")
    if op != "waiting" and not args[0].isdigit():
        raise ValueError(f"not a pull request number: {args[0]}")
    try:
        os.mkdir(directory)
        os.chmod(directory, 0o1777)  # a umask would otherwise keep other users out
    except FileExistsError:
        pass

    def mine(pr: str) -> list[str]:
        """The words of this user's place for `pr`; none when it has no readable one."""
        path = f"{directory}/{pr}"
        try:
            if os.lstat(path).st_uid != os.getuid():
                return []
            with open(path, encoding="utf-8", errors="replace") as handle:
                return handle.read().split()
        except OSError:
            return []

    def write(pr: str, *words: str) -> None:
        # A reader never finds a half-written place, and a link left at the place's name is
        # replaced, not followed.
        scratch = f"{directory}/.{pr}.{os.getpid()}"
        with open(scratch, "x", encoding="utf-8") as handle:
            handle.write(" ".join(words) + "\n")
        os.replace(scratch, f"{directory}/{pr}")

    if op == "join":
        write(*args)
        return ""
    if op == "stand":
        pr, head = args
        words = mine(pr)
        if words[:1] != [head]:
            write(pr, head)
            return ""
        os.utime(f"{directory}/{pr}")
        return words[1] if len(words) > 1 else ""
    if op == "leave":
        if mine(args[0]):
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
            words = mine(name)
            if len(words) == 1:
                found.append(f"{name} {words[0]}")
        return "\n".join(found)
    pr, head, what = args
    if mine(pr) == [head]:
        write(pr, head, what)
    return ""


def here(directory: Path = DIR) -> Line:
    """The line on this host."""
    return lambda op, *args: _op(str(directory), op, *args)


_REMOTE = f"""import os, sys, time
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
    """One landing's place in the line, for the head it waits to test.

    A landing with no head to stand with, and one whose line has failed, has a place that
    does nothing.
    """

    def __init__(
        self, line: Line | None, pr: str, head: str, say: Callable[[str], None]
    ) -> None:
        self._line = line if head else None
        self._pr, self._head, self._say = pr, head, say
        self._stood = False

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

    def stand(self) -> str:
        """Take the place, or refresh it, and return what the holder of the turn answered.

        The first call starts the place anew, so an answer left for an earlier landing of
        the same head is never read as this one's.
        """
        op, self._stood = ("stand" if self._stood else "join"), True
        return self._run(op, self._pr, self._head)

    def answer(self) -> str:
        """What the holder of the turn answered a landing that stood here; "" for any other."""
        return self._run("stand", self._pr, self._head) if self._stood else ""

    def leave(self) -> None:
        """Give the place up, if this landing took it."""
        if self._stood:
            self._run("leave", self._pr)
