"""Move each host-package user to its declared uid, once per host (#8100).

The host packages declare their system users at fixed uids in 900-949, below the uids Ubuntu
hands out from 999 down. A host that held a user at another uid keeps it, because
`systemd-sysusers` creates only a missing user, and reconcile's users arm holds the host with a
fault that names this script. The arm never renumbers an existing user, so the move happens here,
by the operator, once per host.

The declared users come from /usr/lib/homelab/manifest.json, the file reconcile reads, so this
script carries no list of its own. For each user whose uid in /etc/passwd differs from the
declared one, `--apply` runs, in this order:

  1. `systemctl stop` for each service unit running a process as the user;
  2. `usermod -u`, which re-owns the home directory;
  3. `groupmod -g`, when the user's primary group is named like the user and holds the old uid;
  4. `find -uid <old>` with `chown -h`, over the home and the shared directories in SHARED_DIRS;
  5. `systemctl start` for the stopped units, `user@<old>.service` as `user@<new>.service`.

A second run finds every user at its declared uid and runs nothing. Without `--apply` the script
prints each user's old and new uid, the account that already holds the new uid, if any, and every
command it would run. A user is left alone, and the exit status is 1, while another account holds
its new uid or a login session runs as it, which `usermod` refuses.

The exit status is 0 when every user is at its declared uid or was moved, and 1 for anything else:
a refusal, a user left alone or a failed command. The script imports only `lib/cli_help.py` and
`lib/exit_codes.py` from the repo and uses the standard library otherwise, so a host with no
checkout runs it from a copy of those three files at the same relative paths. Delete it, its test
and the pointer in `hosts/lib/reconcile_lib/users.py` once daniel-box, daniel-server and
daniel-pi have each run it.

Run: sudo python3 -B scripts/deploy_tools/renumber_host_users.py [--apply]
`-B` keeps root from writing .pyc files into the checkout.
"""

import json
import os
import shlex
import socket
import subprocess
import sys
import sys as _sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # scripts/
from lib.cli_help import ArgumentParser
from lib.exit_codes import FAILED, OK

MANIFEST = Path("/usr/lib/homelab/manifest.json")
PASSWD = Path("/etc/passwd")
GROUP = Path("/etc/group")
# The shared paths where a user's files can sit outside its home. usermod re-owns its mail spool.
SHARED_DIRS = ("/tmp", "/var/tmp", "/var/spool/cron/crontabs")

Run = Callable[[Sequence[str]], subprocess.CompletedProcess]
Exists = Callable[[str], bool]


@dataclass(frozen=True)
class Account:
    """One declared user, and what the host holds for it."""

    name: str
    new: int
    home: str
    old: int | None = None
    holder: str | None = None
    regroup: bool = False
    group_holder: str | None = None
    units: tuple[str, ...] = ()
    sessions: tuple[str, ...] = ()

    @property
    def state(self) -> str:
        """`absent` (reconcile creates it), `ok`, `blocked` or `move`."""
        if self.old is None:
            return "absent"
        if self.old == self.new:
            return "ok"
        return "blocked" if self.blocker else "move"

    @property
    def blocker(self) -> str | None:
        if self.holder:
            return f"{self.holder} holds uid {self.new}"
        if self.group_holder:
            return f"group {self.group_holder} holds gid {self.new}"
        if self.sessions:
            return f"{', '.join(self.sessions)} runs as it; end the session first"
        return None


def ids(text: str) -> dict[str, int]:
    """Name to id for the rows of an /etc/passwd or /etc/group text."""
    rows = (line.split(":") for line in text.splitlines())
    return {r[0]: int(r[2]) for r in rows if len(r) > 2 and r[2].isdigit()}


def declared_users(manifest: Path) -> list[dict]:
    """The users the host package declares, from the manifest reconcile reads."""
    users = json.loads(manifest.read_text()).get("users", {}).get("users", [])
    if not all(isinstance(u["name"], str) and type(u["uid"]) is int for u in users):
        raise ValueError(
            f"{manifest} has a users entry without a name and an integer uid"
        )
    return users


def holders_of(run: Run, name: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The service units and the login session scopes with a process running as `name`."""
    found = run(["ps", "-o", "unit=", "-u", name])
    units = sorted({unit.strip() for unit in found.stdout.splitlines()} - {""})
    sessions = tuple(
        u for u in units if u.startswith("session-") and u.endswith(".scope")
    )
    return tuple(u for u in units if u.endswith(".service")), sessions


def plan(users: list[dict], passwd: str, group: str, run: Run) -> list[Account]:
    """One `Account` per declared user, with the facts a move needs once it is off its uid."""
    uid_of, gid_of = ids(passwd), ids(group)
    accounts = []
    for user in users:
        name, new, old = user["name"], user["uid"], uid_of.get(user["name"])
        facts: dict = {}
        if old is not None and old != new:
            units, sessions = holders_of(run, name)
            regroup = gid_of.get(name) == old
            facts = {
                "holder": next((n for n, uid in uid_of.items() if uid == new), None),
                "regroup": regroup,
                "group_holder": next(
                    (n for n, gid in gid_of.items() if regroup and gid == new), None
                ),
                "units": units,
                "sessions": sessions,
            }
        accounts.append(Account(name, new, user["home"], old, **facts))
    return accounts


def restart_name(unit: str, move: Account) -> str:
    """The unit to start after the move: the user's manager is named for its uid."""
    return f"user@{move.new}.service" if unit == f"user@{move.old}.service" else unit


def changes(move: Account, exists: Exists) -> list[list[str]]:
    """The commands that renumber the user and re-own its files, in order."""
    roots = [path for path in (move.home, *SHARED_DIRS) if exists(path)]
    find = ["find", *roots, "-xdev"]
    chown = ["-exec", "chown", "-h"]
    argvs = [["usermod", "-u", str(move.new), move.name]]
    if move.regroup:
        argvs.append(["groupmod", "-g", str(move.new), move.name])
    if roots:
        argvs.append([*find, "-uid", str(move.old), *chown, str(move.new), "{}", "+"])
        if move.regroup:
            argvs.append(
                [*find, "-gid", str(move.old), *chown, f":{move.new}", "{}", "+"]
            )
    return argvs


def commands(move: Account, exists: Exists) -> list[list[str]]:
    """Every command a move runs: the stops, the changes and the starts."""
    return [
        *(["systemctl", "stop", unit] for unit in move.units),
        *changes(move, exists),
        *(["systemctl", "start", restart_name(unit, move)] for unit in move.units),
    ]


def apply(move: Account, run: Run, exists: Exists) -> bool:
    """Run the move. The units that were running start again whether or not it succeeded."""
    stopped, ok, moved = [], True, False
    for unit in move.units:
        if run(["systemctl", "stop", unit]).returncode != 0:
            print(f"{move.name}: cannot stop {unit}; nothing changed", file=sys.stderr)
            ok = False
            break
        stopped.append(unit)
    steps = changes(move, exists) if ok else []
    for index, argv in enumerate(steps):
        result = run(argv)
        moved = moved or argv[0] == "usermod"
        if result.returncode != 0:
            print(
                f"{move.name}: {shlex.join(argv)} failed: {result.stderr.strip()}",
                file=sys.stderr,
            )
            if moved:
                left = "; ".join(shlex.join(rest) for rest in steps[index:])
                print(
                    f"{move.name}: the uid moved; finish by hand: {left}",
                    file=sys.stderr,
                )
            ok = False
            break
    for unit in stopped:
        name = restart_name(unit, move) if moved else unit
        if run(["systemctl", "start", name]).returncode != 0:
            print(f"{move.name}: cannot start {name}", file=sys.stderr)
            ok = False
    return ok


def report(account: Account, exists: Exists, applying: bool) -> None:
    """Print one user that needs attention: a move, or a user left alone."""
    holder = account.holder or "none"
    print(
        f"{account.name}: uid {account.old} -> {account.new} (holder of {account.new}: {holder})"
    )
    if account.blocker:
        print(f"  left alone: {account.blocker}")
        return
    print("  running:" if applying else "  would run:")
    for argv in commands(account, exists):
        print(f"    {shlex.join(argv)}")


def main(
    argv: list[str] | None = None,
    *,
    euid: Callable[[], int] = os.geteuid,
    run: Run | None = None,
    manifest: Path = MANIFEST,
    passwd: Path = PASSWD,
    group: Path = GROUP,
    exists: Exists = os.path.isdir,
    host: str | None = None,
) -> int:
    parser = ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--apply",
        action="store_true",
        help="make the changes; the default only prints them",
    )
    args = parser.parse_args(argv)
    if euid() != 0:
        print(
            "renumber_host_users.py must run as root; rerun it with sudo",
            file=sys.stderr,
        )
        return FAILED
    run = run or (
        lambda cmd: subprocess.run(cmd, capture_output=True, text=True, check=False)
    )
    try:
        accounts = plan(
            declared_users(manifest), passwd.read_text(), group.read_text(), run
        )
    except (OSError, ValueError, KeyError) as error:
        print(f"cannot read the declared users: {error}", file=sys.stderr)
        return FAILED
    states = [account.state for account in accounts]
    counts = ", ".join(
        f"{states.count(s)} {s}" for s in ("move", "blocked", "ok", "absent")
    )
    mode = "applying" if args.apply else "dry run, --apply to run"
    print(
        f"{host or socket.gethostname()}: {len(accounts)} declared users: {counts} ({mode})"
    )
    status = OK
    for account in accounts:
        if account.state not in ("move", "blocked"):
            continue
        report(account, exists, args.apply)
        if account.state == "blocked":
            status = FAILED
        elif args.apply and not apply(account, run, exists):
            status = FAILED
    if args.apply and status == 0 and "move" in states:
        print(
            "done; run `sudo /usr/lib/homelab/homelab-updater --clear-hold` to rerun reconcile"
        )
    return status


if __name__ == "__main__":
    sys.exit(main())
