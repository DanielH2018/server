"""The stub commands of reconcile's users, libvirt and lines arms.

They are getent, usermod, ps, find, systemd-sysusers and virsh.

`_reconcile_stub.py` merges `HANDLERS` into its own and documents the rest of the state. These
keys are the ones the three arms read:

    users: {name: uid}. The accounts `getent passwd` knows.
    sysusers: {"creates": {name: uid}, "fail": bool}. What `systemd-sysusers` adds, when it does
        not fail. A name already in `users` keeps its uid, as sysusers leaves an existing user alone.
    expired: names `usermod --expiredate 1` was run for. "fail_usermod": true makes it exit 1.
        `usermod -u <uid> <name>` sets the uid in `users`, and exits 8 for a name in "busy".
    processes: {name: [unit, ...]}. The units `ps -o unit= -u <name>` prints, one per process.
    outside: {path: uid}. Files `find <roots> -uid <old> -exec chown -h <new>` re-owns when the
        path sits under one of the roots it is given. "fail_find": N makes the next N calls exit 1.
    libvirt: {"networks": {name: {"active": bool, "autostart": bool}}, "nwfilters": {name: {}},
        "refuse": [name, ...], "fail": ["net-define", ...]}. `refuse` names the objects libvirt
        will not undefine, as for a filter a running guest references. `fail` names the verbs that
        exit 1. A define reads the object's name from the `<name>` element of the file it is given.
    conffiles: {package: [path, ...]}. What `dpkg-query -W -f '${Conffiles}'` lists. `owned`
        in `_reconcile_stub.py` names the paths `dpkg -S` reports as owned by `some-package`.
"""

import re
import sys
from pathlib import Path

NOT_FOUND = 2


def getent(state: dict, args: list[str]) -> int:
    name = args[1]
    if name not in state.get("users", {}):
        return NOT_FOUND
    print(
        f"{name}:x:{state['users'][name]}:{state['users'][name]}::/home/{name}:/bin/sh"
    )
    return 0


def usermod(state: dict, args: list[str]) -> int:
    if state.get("fail_usermod"):
        print("usermod: stub refuses", file=sys.stderr)
        return 1
    if args[0] == "-u":
        if args[2] in state.get("busy", []):
            print(
                f"usermod: user {args[2]} is currently used by process 4242",
                file=sys.stderr,
            )
            return 8
        state["users"][args[2]] = int(args[1])
        return 0
    state.setdefault("expired", []).append(args[-1])
    return 0


def find(state: dict, args: list[str]) -> int:
    if state.get("fail_find"):
        state["fail_find"] -= 1
        print("find: '/tmp/x': No such file or directory", file=sys.stderr)
        return 1
    roots = args[: args.index("-xdev")]
    old, new = int(args[args.index("-uid") + 1]), int(args[args.index("-h") + 1])
    outside = state.get("outside", {})
    for path, uid in outside.items():
        if uid == old and any(path.startswith(root + "/") for root in roots):
            outside[path] = new
    return 0


def ps(state: dict, args: list[str]) -> int:
    for unit in state.get("processes", {}).get(args[-1], []):
        print(f"  {unit}")
    return 0


def systemd_sysusers(state: dict, args: list[str]) -> int:
    model = state.get("sysusers", {})
    if model.get("fail"):
        print("systemd-sysusers: stub fails", file=sys.stderr)
        return 1
    for name, uid in model.get("creates", {}).items():
        state.setdefault("users", {}).setdefault(name, uid)
    return 0


def virsh(state: dict, args: list[str]) -> int:
    model = state.setdefault("libvirt", {})
    verb, rest = args[2], args[3:]
    if verb in model.get("fail", []):
        print(f"error: stub refuses {verb}", file=sys.stderr)
        return 1
    kind = "nwfilters" if verb.startswith("nwfilter") else "networks"
    objects = model.setdefault(kind, {})
    if verb.endswith("-define"):
        xml = Path(rest[0]).read_text()
        pattern = (
            r"<filter name='(.*?)'" if kind == "nwfilters" else r"<name>(.*?)</name>"
        )
        name = re.findall(pattern, xml)[0]
        objects.setdefault(name, {"active": False, "autostart": False})
        return 0
    name = rest[0]
    if name not in objects:
        print(f"error: failed to get {name}", file=sys.stderr)
        return 1
    if verb in ("net-info", "nwfilter-dumpxml"):
        print(f"Name: {name}\nActive: {'yes' if objects[name].get('active') else 'no'}")
    elif verb == "net-autostart":
        objects[name]["autostart"] = True
    elif verb == "net-start":
        objects[name]["active"] = True
    elif verb in ("net-undefine", "nwfilter-undefine"):
        if name in model.get("refuse", []):
            print(
                f"error: Failed to undefine {name}: in use by a guest", file=sys.stderr
            )
            return 1
        del objects[name]
    else:
        raise SystemExit(f"stub virsh: unexpected {args}")
    return 0


def conffiles(state: dict, package: str) -> int:
    for path in state.get("conffiles", {}).get(package, []):
        print(f" {path} 0123456789abcdef0123456789abcdef")
    return 0


HANDLERS = {
    "getent": getent,
    "usermod": usermod,
    "ps": ps,
    "find": find,
    "systemd-sysusers": systemd_sysusers,
    "virsh": virsh,
}
