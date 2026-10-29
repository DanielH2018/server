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

The arm never deletes a user. A user that leaves the list is expired with `usermod --expiredate
1`, and removing its home is an operator step. The arm expires only a name its own record
holds, and it expires nothing while the declared list is empty, so a revert to a version that
declares no users leaves them alone. Each name the floor kept is drift.
"""

import re
from pathlib import Path

from reconcile_lib.common import ERR, run, log, under_root

SYSUSERS = Path("/usr/lib/sysusers.d/homelab.conf")
NAME = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
KEYS = {"name", "uid", "home", "shell", "comment"}


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
        and all(set(u) == KEYS for u in users)
        and all(isinstance(u["name"], str) and NAME.fullmatch(u["name"]) for u in users)
        and all(type(u["uid"]) is int and 0 < u["uid"] < 65534 for u in users)
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
    """`live` when the user exists with the declared uid, `absent` when missing, else `fault`."""
    found = run("getent", "passwd", user["name"])
    if found.returncode == 2:
        return "absent"
    fields = found.stdout.split(":")
    if found.returncode != 0 or len(fields) < 3 or not fields[2].isdigit():
        log(f"getent passwd {user['name']} failed: {found.stderr.strip()}", ERR)
        return "fault"
    if int(fields[2]) != user["uid"]:
        log(
            f"{user['name']} has uid {fields[2]}; the package declares {user['uid']}",
            ERR,
        )
        return "fault"
    return "live"


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
    missing = [u for u in users if verdicts[u["name"]] == "absent"]
    if ok and missing:
        created = run("systemd-sysusers", str(under_root(SYSUSERS))).returncode == 0
        ok = created and all(check(u) == "live" for u in missing)
        if not ok:
            log("systemd-sysusers did not leave every declared user in place", ERR)
    names = {user["name"] for user in users}
    stale = sorted(set((prev or {}).get("names") or []) - names)
    drift = 0
    for name in stale:
        if not users:
            names.add(name)
            drift += 1
        elif not expire(name):
            names.add(name)
            ok = False
    return ok, {"names": sorted(names)}, drift
