"""The stub commands of reconcile's users, libvirt and lines arms: getent, usermod, sysusers, virsh.

`_reconcile_stub.py` merges `HANDLERS` into its own and documents the rest of the state. These
keys are the ones the three arms read:

    users: {name: uid}. The accounts `getent passwd` knows.
    sysusers: {"creates": {name: uid}, "fail": bool}. What `systemd-sysusers` adds, when it does
        not fail. A name already in `users` keeps its uid, as sysusers leaves an existing user alone.
    expired: names `usermod --expiredate 1` was run for. "fail_usermod": true makes it exit 1.
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
    state.setdefault("expired", []).append(args[-1])
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
        name = re.findall(r"<name>(.*?)</name>", Path(rest[0]).read_text())[0]
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
    "systemd-sysusers": systemd_sysusers,
    "virsh": virsh,
}
