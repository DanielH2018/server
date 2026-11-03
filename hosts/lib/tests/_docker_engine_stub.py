"""Stub apt, dpkg, systemctl and docker for `hosts/components/pi-docker/docker-engine`.

Each stub command runs `python3 _docker_engine_stub.py <command> <args>`, records the call in
`calls.jsonl` and reads and writes `state.json`, both under `$STUB_STATE`. The state:

    installed: package -> the version dpkg reports. A missing package is not installed.
    offered: package -> versions `apt-cache madison` lists, newest first.
    held: packages `apt-mark hold` has held.
    timer: homelab-updater.timer's `is-enabled` answer, "enabled" or "masked".
    active: unit -> how many more polls find the unit running.
    running_state: the ActiveState a running unit reports, "activating" unless a test sets it,
        because the units the engine waits on are oneshots.
    install_exit: what `apt-get install` exits; any other value than 0 installs nothing.
    modified_conffile: a conffile on the host differs from the packaged copy, so an install
        without `--force-confold` stops at dpkg's prompt, which fails with no terminal.
"""

import json
import os
import sys
from pathlib import Path

STATE = Path(os.environ["STUB_STATE"])


def dpkg_query(state: dict, args: list[str]) -> int:
    version = state["installed"].get(args[-1])
    if version is None:
        return 1
    print(version, end="")
    return 0


def apt_cache(state: dict, args: list[str]) -> int:
    for version in state["offered"].get(args[1], []):
        print(
            f"{args[1]} | {version} | https://download.docker.com/linux/ubuntu noble/stable"
        )
    return 0


def apt_get(state: dict, args: list[str]) -> int:
    if args[0] != "install":
        return 0
    if state["install_exit"]:
        return state["install_exit"]
    if state.get("modified_conffile") and "Dpkg::Options::=--force-confold" not in args:
        print("dpkg: error: end of file on stdin at conffile prompt", file=sys.stderr)
        return 100
    for spec in (a for a in args if "=" in a and "::" not in a):
        package, _, version = spec.partition("=")
        state["installed"][package] = version
    return 0


def apt_mark(state: dict, args: list[str]) -> int:
    state["held"] = sorted({*state["held"], args[1]})
    return 0


def systemctl(state: dict, args: list[str]) -> int:
    if args[0] == "is-enabled":
        print(state["timer"])
        return 0
    if args[0] == "mask":
        state["timer"] = "masked"
    elif args[0] == "unmask":
        state["timer"] = "enabled"
    elif args[0] == "show" or args[:2] == ["-q", "is-active"]:
        unit = args[-1]
        left = state["active"].get(unit, 0)
        state["active"][unit] = max(left - 1, 0)
        running = state.get("running_state", "activating")
        if args[0] == "show":
            print(running if left else "inactive")
            return 0
        # systemctl's own codes: it exits 3 for `activating` and `deactivating`.
        return 0 if left and running in ("active", "reloading") else 3
    return 0


def nothing(state: dict, args: list[str]) -> int:
    return 0


HANDLERS = {
    "dpkg-query": dpkg_query,
    "apt-cache": apt_cache,
    "apt-get": apt_get,
    "apt-mark": apt_mark,
    "systemctl": systemctl,
    "docker": nothing,
    "sleep": nothing,
}


def main(command: str, args: list[str]) -> int:
    state = json.loads((STATE / "state.json").read_text())
    with (STATE / "calls.jsonl").open("a") as calls:
        calls.write(json.dumps([command, *args]) + "\n")
    code = HANDLERS[command](state, args)
    (STATE / "state.json").write_text(json.dumps(state))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2:]))
