"""The users arm: the system users a host package declares, each with a fixed uid.

The design is docs/target-architecture/host/h7-delete-ansible.md, *Reconcilers for state dpkg
does not hold*. A host package's manifest lists "users" in `reconcilers` and carries:

    "users": {"users": [{"name": "claude", "uid": 1001, "home": "/home/claude",
                         "shell": "/bin/bash", "comment": "Claude agent"}, ...]}

The renderer writes the same list to /usr/lib/sysusers.d/homelab.conf, and the arm creates the
missing users with `systemd-sysusers`. sysusers creates only users that are missing, so a user
that exists with another uid would keep it. The arm therefore compares each declared uid with
`getent passwd` first. A mismatch is a fault: the package shipped a wrong uid, and the run fails
and holds the host. A user still missing after sysusers is a fault too.

An entry may carry `moved_from`, the uid the user held before the package renumbered it. A live
uid equal to `moved_from` is a declared move, not a fault. The arm stops the `.service` units
running as the user, such as a lingering `user@<old>.service`, and runs `usermod -u`, which
re-owns the files under the home directory, and starts the units again. It then re-owns the old
uid's files in /tmp, /var/tmp and the crontab spool. A re-own that fails, as find does when a
file it listed vanishes before chown runs, is drift: the record keeps the old uid, and each later
run retries the re-own until it succeeds. The user's primary group keeps its gid. A failed move
starts the stopped units again under the old uid and faults. A login session
running as the user makes usermod refuse, so the arm then stops nothing and counts the move as
drift, which the next run retries.

The arm never deletes a user. A user that leaves the list is expired with `usermod --expiredate
1`, and removing its home is an operator step. The arm expires only a name its own record
holds, and it expires nothing while the declared list is empty, so a revert to a version that
declares no users leaves them alone. Each name the floor kept is drift.
"""

import re
from pathlib import Path

from reconcile_lib.common import ERR, WARNING, run, log, under_root

SYSUSERS = Path("/usr/lib/sysusers.d/homelab.conf")
NAME = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
KEYS = {"name", "uid", "home", "shell", "comment"}
MOVE_KEYS = KEYS | {"moved_from"}
# The shared paths where a moved user's files can sit; usermod re-owns its mail spool itself.
OUTSIDE_HOME = ("/tmp", "/var/tmp", "/var/spool/cron/crontabs")


def valid(section: object) -> bool:
    """True when `section` is a well-formed manifest `users` object."""
    if not isinstance(section, dict) or set(section) != {"users"}:
        return False
    users = section["users"]
    if not isinstance(users, list):
        return False
    names = [u.get("name") for u in users if isinstance(u, dict)]
    uids = [u.get("uid") for u in users if isinstance(u, dict)]
    return (
        len(names) == len(users) == len(set(names)) == len(set(uids))
        and all(set(u) in (KEYS, MOVE_KEYS) for u in users)
        and all(isinstance(u["name"], str) and NAME.fullmatch(u["name"]) for u in users)
        and all(type(u["uid"]) is int and 0 < u["uid"] < 65534 for u in users)
        and all(
            type(u["moved_from"]) is int
            and 0 < u["moved_from"] < 65534
            and u["moved_from"] != u["uid"]
            for u in users
            if "moved_from" in u
        )
        and all(
            isinstance(u[key], str) and u[key].startswith("/") and ".." not in u[key]
            for u in users
            for key in ("home", "shell")
        )
        and all(
            isinstance(u["comment"], str) and not set('"\n\\') & set(u["comment"])
            for u in users
        )
    )


def check(user: dict) -> str:
    """`live` at the declared uid, `move` at its `moved_from`, `absent` when missing, else `fault`."""
    found = run("getent", "passwd", user["name"])
    if found.returncode == 2:
        return "absent"
    fields = found.stdout.split(":")
    if found.returncode != 0 or len(fields) < 3 or not fields[2].isdigit():
        log(f"getent passwd {user['name']} failed: {found.stderr.strip()}", ERR)
        return "fault"
    if int(fields[2]) == user.get("moved_from"):
        return "move"
    if int(fields[2]) != user["uid"]:
        log(
            f"{user['name']} has uid {fields[2]}; the package declares {user['uid']}",
            ERR,
        )
        return "fault"
    return "live"


def holders(name: str) -> tuple[list[str], list[str]]:
    """The `.service` units and the login `session-*.scope` units running a process as `name`.

    A container that runs as the same uid sits in another `.scope`, and usermod ignores it because
    its root differs, so the arm leaves it alone.
    """
    found = run("ps", "-o", "unit=", "-u", name)
    units = sorted({unit.strip() for unit in found.stdout.splitlines()})
    sessions = [u for u in units if u.startswith("session-") and u.endswith(".scope")]
    return [u for u in units if u.endswith(".service")], sessions


def rehome(old: int, new: int) -> bool:
    """Re-own the files outside the home that still carry `old`; usermod re-owns only the home."""
    roots = [str(under_root(Path(p))) for p in OUTSIDE_HOME]
    roots = [root for root in roots if Path(root).is_dir()]
    if not roots:
        return True
    found = run(
        "find",
        *roots,
        "-xdev",
        "-uid",
        str(old),
        "-exec",
        "chown",
        "-h",
        str(new),
        "{}",
        "+",
    )
    if found.returncode != 0:
        log(
            f"cannot re-own the files uid {old} holds outside its home yet: {found.stderr.strip()}",
            WARNING,
        )
    return found.returncode == 0


def move(user: dict) -> str:
    """Renumber `user` from its `moved_from` uid to its declared uid.

    Returns:
        `moved`; `waiting` when a login session runs as the user, which usermod refuses and the
        next run retries; or `failed`, after which the stopped units run again under the old uid.
    """
    name, old, new = user["name"], user["moved_from"], user["uid"]
    units, sessions = holders(name)
    if sessions:
        log(f"{name} stays at uid {old} while {', '.join(sessions)} runs as it")
        return "waiting"
    # DECIDED: the arm stops a user's units only for a move the package declares (#8100).
    stopped = [unit for unit in units if run("systemctl", "stop", unit).returncode == 0]
    moved = len(stopped) == len(units)
    if not moved:
        log(f"cannot stop every unit running as {name}: {', '.join(units)}", ERR)
    else:
        result = run("usermod", "-u", str(new), name)
        moved = result.returncode == 0
        if not moved:
            log(f"usermod -u {new} {name} failed: {result.stderr.strip()}", ERR)
    for unit in stopped:
        if moved and unit == f"user@{old}.service":
            unit = f"user@{new}.service"
        if run("systemctl", "start", unit).returncode != 0:
            log(f"cannot start {unit} after the move of {name}", ERR)
            moved = False
    if moved:
        log(f"moved {name} from uid {old} to {new}")
    return "moved" if moved and check(user) == "live" else "failed"


def expire(name: str) -> bool:
    gone = run("getent", "passwd", name).returncode == 2
    if gone or run("usermod", "--expiredate", "1", name).returncode == 0:
        return True
    log(f"cannot expire {name}", ERR)
    return False


def reconcile(section: dict | None, prev: dict | None) -> tuple[bool, dict | None, int]:
    """Run the arm. Return whether it succeeded, its record for applied.json, and its drift."""
    if section is None and prev is None:
        return True, None, 0
    users = section["users"] if section else []
    verdicts = {user["name"]: check(user) for user in users}
    ok = "fault" not in verdicts.values()
    moving = [u for u in users if ok and verdicts[u["name"]] == "move"]
    moves = {u["name"]: move(u) for u in moving}
    ok = ok and "failed" not in moves.values()
    # A re-own that failed stays in the record, so each run retries it until it succeeds.
    pending = {
        u["name"]: u["moved_from"] for u in moving if moves[u["name"]] == "moved"
    }
    for name, old in ((prev or {}).get("rehome") or {}).items():
        if verdicts.get(name) == "live" and name not in pending:
            pending[name] = old
    uids = {u["name"]: u["uid"] for u in users}
    rehome_left = {
        n: old for n, old in sorted(pending.items()) if not rehome(old, uids[n])
    }
    missing = [u for u in users if verdicts[u["name"]] == "absent"]
    if ok and missing:
        created = run("systemd-sysusers", str(under_root(SYSUSERS))).returncode == 0
        ok = created and all(check(u) == "live" for u in missing)
        if not ok:
            log("systemd-sysusers did not leave every declared user in place", ERR)
    names = {user["name"] for user in users}
    stale = sorted(set((prev or {}).get("names") or []) - names)
    drift = list(moves.values()).count("waiting") + len(rehome_left)
    for name in stale:
        if not users:
            names.add(name)
            drift += 1
        elif not expire(name):
            names.add(name)
            ok = False
    record = {"names": sorted(names)} | ({"rehome": rehome_left} if rehome_left else {})
    return ok, record, drift
