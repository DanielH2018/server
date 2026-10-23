"""One stub for every command `homelab-reconcile` runs, dispatched on the name it is called as.

`_reconcile_host.py` puts a wrapper per command on reconcile's `PATH`. Each wrapper runs
`_reconcile_stub.py <command> <args>`. The stub reads the host it fakes from
`$STUB_STATE/state.json`, writes back what a call changed, and appends one line per call to
`$STUB_STATE/calls.jsonl`: the argv, and whether dpkg's frontend lock was free at that moment.

state.json keys:
    host: what `hostname` prints.
    installed: package -> [three-letter dpkg status, version].
    units: unit -> {"load": loaded|masked|not-found, "active": active|inactive|failed,
        "enabled": enabled|disabled|static}. A unit missing here is not loaded.
    fail: "<verb> <unit>" strings, each a systemctl call that exits 1.
    owned: paths `dpkg -S` reports a package owns.
    crontabs: user -> crontab text. A user missing here has no crontab.
    effects: "<verb> <unit>" -> {"write": {absolute path: text}, "queue": [unit, ...]}, what a
        successful call of that verb leaves on disk under HOMELAB_ROOT: the files it writes and
        the units it adds to /run/homelab-secrets/pending.json.
    A unit's "limit": true makes `start` and `restart` fail until `reset-failed` clears it.
"""

import fcntl
import fnmatch
import json
import os
import sys
from pathlib import Path

STATE = Path(os.environ["STUB_STATE"])
LOCK = Path(os.environ["HOMELAB_ROOT"]) / "var/lib/dpkg/lock-frontend"


def main(command: str, args: list[str]) -> int:
    state = json.loads((STATE / "state.json").read_text())
    with (STATE / "calls.jsonl").open("a") as calls:
        calls.write(
            json.dumps({"argv": [command, *args], "lock_free": _lock_free()}) + "\n"
        )
    code = HANDLERS[command](state, args)
    (STATE / "state.json").write_text(json.dumps(state))
    return code


def _lock_free() -> bool:
    if not LOCK.exists():
        return True
    with LOCK.open("r+") as handle:
        try:
            fcntl.lockf(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
    return True


def hostname(state: dict, args: list[str]) -> int:
    print(state["host"])
    return 0


def dpkg_query(state: dict, args: list[str]) -> int:
    code = 0
    for package in args[3:]:
        if package in state["installed"]:
            status, version = state["installed"][package]
            print(f"{package} {status}{version}")
        else:
            print(f"dpkg-query: no packages found matching {package}", file=sys.stderr)
            code = 1
    return code


def dpkg(state: dict, args: list[str]) -> int:
    if args[0] != "-S":
        raise SystemExit(f"stub dpkg: unexpected {args}")
    if args[1] in state["owned"]:
        print(f"some-package: {args[1]}")
        return 0
    print(f"dpkg-query: no path found matching pattern {args[1]}", file=sys.stderr)
    return 1


def crontab(state: dict, args: list[str]) -> int:
    user = args[args.index("-u") + 1]
    if "-l" in args:
        if user not in state["crontabs"]:
            print(f"no crontab for {user}", file=sys.stderr)
            return 1
        print(state["crontabs"][user], end="")
        return 0
    state["crontabs"][user] = sys.stdin.read()
    return 0


def systemctl(state: dict, args: list[str]) -> int:
    verb, rest = args[0], args[1:]
    units = state["units"]
    if verb == "show":
        return _show(units, rest)
    if verb == "daemon-reload":
        return 0
    (unit,) = rest
    if f"{verb} {unit}" in state["fail"]:
        print(f"Job for {unit} failed.", file=sys.stderr)
        return 1
    known = units.get(unit)
    if verb == "is-active":
        print(known["active"] if known else "inactive")
        return 0 if known and known["active"] == "active" else 3
    if verb == "is-enabled":
        print(known["enabled"] if known else "not-found")
        return 0 if known and known["enabled"] == "enabled" else 1
    if known is None:
        print(f"Unit {unit} not loaded.", file=sys.stderr)
        return 5
    if verb == "stop":
        known["active"] = "inactive"
    elif verb in ("start", "restart"):
        if known["load"] != "loaded":
            print(f"Unit {unit} is {known['load']}.", file=sys.stderr)
            return 1
        if known.get("limit"):
            print(f"{unit}: start request repeated too quickly", file=sys.stderr)
            return 1
        known["active"] = "active"
        _effects(state, f"{verb} {unit}")
    elif verb == "reset-failed":
        known.pop("limit", None)
    elif verb == "try-restart":
        pass
    elif verb == "disable":
        known["enabled"] = "disabled"
    else:
        raise SystemExit(f"stub systemctl: unexpected {args}")
    return 0


def _effects(state: dict, call: str) -> None:
    root = Path(os.environ["HOMELAB_ROOT"])
    effect = state.get("effects", {}).get(call, {})
    for path, text in effect.get("write", {}).items():
        target = root / path.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    if effect.get("queue"):
        pending = root / "run/homelab-secrets/pending.json"
        pending.parent.mkdir(parents=True, exist_ok=True)
        queue = json.loads(pending.read_text()) if pending.exists() else []
        pending.write_text(json.dumps(sorted(set(queue) | set(effect["queue"]))))


def _show(units: dict, args: list[str]) -> int:
    names = [arg for i, arg in enumerate(args) if arg != "-p" and args[i - 1] != "-p"]
    if "--value" in names:
        (unit,) = [name for name in names if name != "--value"]
        known = units.get(unit)
        print(known["load"] if known else "not-found")
        return 0
    (pattern,) = names
    blocks = [
        f"Id={unit}\nLoadState={known['load']}\n"
        for unit, known in sorted(units.items())
        if fnmatch.fnmatch(unit, pattern)
    ]
    print("\n".join(blocks), end="")
    return 0


HANDLERS = {
    "hostname": hostname,
    "dpkg-query": dpkg_query,
    "dpkg": dpkg,
    "crontab": crontab,
    "systemctl": systemctl,
}

if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2:]))
