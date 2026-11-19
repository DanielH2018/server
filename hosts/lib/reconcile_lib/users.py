"""The users arm: the system users a host package declares, each with a fixed uid and gid.

The design is docs/archive/host/h7-delete-ansible.md, *Reconcilers for state dpkg
does not hold*. A host package's manifest lists "users" in `reconcilers` and carries:

    "users": {"users": [{"name": "claude", "uid": 1001, "gid": 1001, "home": "/home/claude",
                         "shell": "/bin/bash", "comment": "Claude agent"}, ...]}

`gid` is the gid of the user's own same-named group, which is its primary group.

The renderer writes the same list to /usr/lib/sysusers.d/homelab.conf, a `g` line and a `u` line
per user, and the arm creates the missing users with `systemd-sysusers`. sysusers creates only
users that are missing, so a user that exists with another uid or gid would keep it. The arm
therefore compares each declared uid and gid with `getent passwd`, and the gid with `getent
group` for the user's own group, first. A mismatch is a fault:
the package shipped a wrong id, and the run fails and holds the host. The arm never renumbers an
existing user or group. A user still missing after sysusers is a fault too.

The arm never deletes a user. A user that leaves the list is expired with `usermod --expiredate
1`, and removing its home is an operator step. The arm expires only a name its own record
holds, and it expires nothing while the declared list is empty, so a revert to a version that
declares no users leaves them alone. Each name the floor kept is drift.

The record is {"names": [...], "expired": [...]}. `names` holds the declared users and the
ones the arm has yet to expire. `expired` holds each name the arm expired that the package does
not declare again. The weekly AIDE check reads it to explain the expiry date `usermod` writes.
"""

import re
from pathlib import Path

from reconcile_lib.common import ERR, run, log, under_root

SYSUSERS = Path("/usr/lib/sysusers.d/homelab.conf")
NAME = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
KEYS = {"name", "uid", "gid", "home", "shell", "comment"}
IDS = ("uid", "gid")


def valid(section: object) -> bool:
    """True when `section` is a well-formed manifest `users` object."""
    if not isinstance(section, dict) or set(section) != {"users"}:
        return False
    users = section["users"]
    if not isinstance(users, list):
        return False
    ids = [[u.get(k) for u in users if isinstance(u, dict)] for k in ("name", *IDS)]
    return (
        all(len(users) == len(column) == len(set(column)) for column in ids)
        and all(set(u) == KEYS for u in users)
        and all(isinstance(u["name"], str) and NAME.fullmatch(u["name"]) for u in users)
        and all(type(u[k]) is int and 0 < u[k] < 65534 for u in users for k in IDS)
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
    """`live` when the user exists with the declared ids, `absent` when missing, else `fault`."""
    found = run("getent", "passwd", user["name"])
    if found.returncode == 2:
        return "absent"
    fields = found.stdout.split(":")
    numbers = fields[2:4]
    if found.returncode or len(numbers) < 2 or not all(map(str.isdigit, numbers)):
        log(f"getent passwd {user['name']} failed: {found.stderr.strip()}", ERR)
        return "fault"
    name, home = user["name"], user["home"]
    if int(fields[2]) != user["uid"]:
        log(
            f"{name} has uid {fields[2]}; the package declares {user['uid']}. To move it, as root: stop every unit running as {name}; usermod -u {user['uid']} {name}; find {home} /tmp /var/tmp /var/spool/cron/crontabs -xdev -uid {fields[2]} -exec chown -h {user['uid']} {{}} +; start the units again; then sudo /usr/lib/homelab/homelab-updater --clear-hold",
            ERR,
        )
        return "fault"
    group = run("getent", "group", name).stdout.split(":")
    group_gid = group[2] if len(group) > 2 and group[2].isdigit() else "none"
    if int(fields[3]) != user["gid"] or group_gid != str(user["gid"]):
        log(
            f"{name} has gid {fields[3]} and group {name} has gid {group_gid}; the package declares {user['gid']} for both. To move them, as root: stop every unit running as {name}; groupmod -g {user['gid']} {name}; usermod -g {user['gid']} {name}; find {home} /tmp /var/tmp /var/spool/cron/crontabs -xdev -gid {fields[3]} -exec chown -h :{user['gid']} {{}} +; start the units again; then sudo /usr/lib/homelab/homelab-updater --clear-hold",
            ERR,
        )
        return "fault"
    return "live"


def expire(name: str) -> str:
    """`expired`, `gone` when no such user exists, or `failed`."""
    if run("getent", "passwd", name).returncode == 2:
        return "gone"
    if run("usermod", "--expiredate", "1", name).returncode == 0:
        return "expired"
    log(f"cannot expire {name}", ERR)
    return "failed"


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
    expired = set((prev or {}).get("expired") or []) - names
    drift = 0
    for name in stale:
        if not users:
            names.add(name)
            drift += 1
        elif (outcome := expire(name)) == "expired":
            expired.add(name)
        elif outcome == "failed":
            names.add(name)
            ok = False
    return ok, {"names": sorted(names), "expired": sorted(expired)}, drift
