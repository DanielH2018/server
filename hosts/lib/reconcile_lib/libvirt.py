"""The libvirt arm: the networks and nwfilters a host package declares, and no others.

The design is docs/target-architecture/host/h7-delete-ansible.md, *Reconcilers for state dpkg
does not hold*. A host package's manifest lists "libvirt" in `reconcilers` and carries:

    "libvirt": {"networks":  [{"name": "staging", "path": "/usr/lib/homelab/libvirt/networks/staging.xml"}],
                "nwfilters": [{"name": "staging-egress-fence", "path": "/usr/lib/homelab/libvirt/nwfilters/..."}]}

Each file is the object's XML with its name and a pinned UUID, which the renderer checks. The arm
defines an object when its XML hash differs from the one `applied.json` holds, or when libvirt
does not know the object. `net-define` and `nwfilter-define` both replace a definition of the same
name and UUID, so no rename happens and a define cannot collide. After it defines a network, the
arm sets autostart and starts the network when it is not active.

The arm undefines only an object whose name its own record holds and the manifest no longer
declares, never one matched by prefix, so libvirt's `default` network is never touched. It
undefines nothing while the manifest declares no object, so a revert to a version that declares
none leaves them alone. libvirt refuses to undefine a filter a running guest references. The arm
leaves such an object recorded, counts it as drift, and does not retry within the run, so the
run succeeds and a revert still updates the host. A failed define is a fault.

The arm reads `applied.json`'s `libvirt` record as {"networks": {name: sha256}, "nwfilters":
{name: sha256}}. An object advances in it only after its define, autostart and start all
succeeded.
"""

import hashlib
import re
from pathlib import Path

from reconcile_lib.common import ERR, run, log, under_root

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
# Per kind: the virsh verb prefix, and the command that fails for an object libvirt lacks.
KINDS = {
    "nwfilters": ("nwfilter", "nwfilter-dumpxml"),
    "networks": ("net", "net-info"),
}


def valid(section: object) -> bool:
    """True when `section` is a well-formed manifest `libvirt` object."""
    return (
        isinstance(section, dict)
        and set(section) == set(KINDS)
        and all(isinstance(section[kind], list) for kind in KINDS)
        and all(
            isinstance(o, dict)
            and set(o) == {"name", "path"}
            and isinstance(o["name"], str)
            and NAME.fullmatch(o["name"])
            and isinstance(o["path"], str)
            and o["path"].startswith("/")
            and ".." not in o["path"].split("/")
            for kind in KINDS
            for o in section[kind]
        )
        and all(
            len({o["name"] for o in section[kind]}) == len(section[kind])
            for kind in KINDS
        )
    )


def virsh(*args: str):
    return run("virsh", "--connect", "qemu:///system", *args)


def known(kind: str, name: str) -> bool:
    return virsh(KINDS[kind][1], name).returncode == 0


def start(name: str) -> bool:
    """Make a defined network start at boot, and run now."""
    ok = virsh("net-autostart", name).returncode == 0
    if not re.search(r"^Active:\s+yes", virsh("net-info", name).stdout, re.M):
        ok = virsh("net-start", name).returncode == 0 and ok
    return ok


def define(kind: str, name: str, path: str) -> bool:
    verb = KINDS[kind][0]
    if virsh(f"{verb}-define", str(under_root(Path(path)))).returncode != 0:
        log(f"{verb}-define {name} failed", ERR)
        return False
    return kind != "networks" or start(name)


def converge(kind: str, entries: list[dict], old: dict) -> tuple[dict, bool]:
    """Define each declared object that is new or changed. Return its record and whether all held."""
    record, ok = {}, True
    for entry in entries:
        name = entry["name"]
        try:
            digest = hashlib.sha256(under_root(Path(entry["path"])).read_bytes())
        except OSError as exc:
            log(f"cannot read {entry['path']}: {exc}", ERR)
            ok = False
        else:
            if old.get(name) == digest.hexdigest() and known(kind, name):
                record[name] = old[name]
            elif define(kind, name, entry["path"]):
                record[name] = digest.hexdigest()
            else:
                ok = False
        if name not in record and name in old:
            record[name] = old[name]
    return record, ok


def undefine(kind: str, name: str) -> bool:
    verb = KINDS[kind][0]
    done = virsh(f"{verb}-undefine", name).returncode == 0
    if not done:
        log(f"{verb}-undefine {name} refused; leaving it recorded as drift", ERR)
    return done


def reconcile(section: dict | None, prev: dict | None) -> tuple[bool, dict | None, int]:
    """Run the arm. Return whether it succeeded, its record for applied.json, and its drift."""
    if section is None and prev is None:
        return True, None, 0
    ok, drift, record = True, 0, {}
    # The floor: with nothing declared, every recorded object is kept and counted as drift.
    floor = not section or not any(section[kind] for kind in KINDS)
    for kind in KINDS:
        entries = section[kind] if section else []
        old = (prev or {}).get(kind)
        old = old if isinstance(old, dict) else {}
        record[kind], held = converge(kind, entries, old)
        ok = ok and held
        for name in sorted(set(old) - {e["name"] for e in entries}):
            if floor or (known(kind, name) and not undefine(kind, name)):
                record[kind][name] = old[name]
                drift += 1
    return ok, record, drift
