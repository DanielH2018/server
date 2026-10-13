"""One stub for every command `homelab-updater` runs, dispatched on the name it is called as.

`test_homelab_updater.py` puts a wrapper per command on the updater's `PATH`. Each wrapper runs
`_updater_stub.py <command> <args>`. The stub reads the host it fakes from `$STUB_STATE/state.json`,
writes back what an install or a reconcile changed, and appends its argv to
`$STUB_STATE/calls.jsonl`, so a test asserts on what the updater asked for.

state.json keys:
    host: what `hostname` prints.
    installed: package -> [three-letter dpkg status, version].
    repo: package -> versions the homelab repository publishes, or null for a fetch that
        writes no Packages list.
    update_exit, full_update_exit: the restricted and the full `apt-get update`'s exits.
    install_failures: how many `apt-get install` calls fail before one succeeds.
    audit, configure_exit, configure_output: `dpkg --audit` and `dpkg --configure -a`.
    reconcile: "ok" or "fail", the run `systemctl start homelab-reconcile.service` records.
    verify: what `dpkg --verify` prints.
    hour, today, now: what `date +%H`, `+%F` and `+%s` print.

`dpkg --compare-versions` runs the real dpkg, so the version order is Debian's.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

STATE = Path(os.environ["STUB_STATE"])
ROOT = Path(os.environ["HOMELAB_ROOT"])
LIST = STATE / "lists" / "homelab_dists_homelab_main_binary-all_Packages"
RECORD = ROOT / "var/lib/homelab-reconcile/applied.json"


def main(command: str, args: list[str]) -> int:
    state = json.loads((STATE / "state.json").read_text())
    with (STATE / "calls.jsonl").open("a") as calls:
        calls.write(json.dumps([command, *args]) + "\n")
    code = HANDLERS[command](state, args)
    (STATE / "state.json").write_text(json.dumps(state))
    return code


def hostname(state: dict, args: list[str]) -> int:
    print(state["host"])
    return 0


def date(state: dict, args: list[str]) -> int:
    print({"+%H": state["hour"], "+%F": state["today"], "+%s": state["now"]}[args[0]])
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
    if args[0] == "--compare-versions":
        return subprocess.run(["/usr/bin/dpkg", *args], check=False).returncode
    if args[0] == "--audit":
        print(state["audit"], end="")
        return 0
    if args[:2] == ["--configure", "-a"]:
        print(state["configure_output"], end="")
        if state["configure_exit"] == 0:
            state["audit"] = ""
        return state["configure_exit"]
    if args[0] == "--verify":
        print(state["verify"], end="")
        return 0
    raise SystemExit(f"stub dpkg: unexpected {args}")


def apt_get(state: dict, args: list[str]) -> int:
    if args[0] == "update":
        restricted = any(arg.startswith("Dir::Etc::sourceparts") for arg in args)
        code = state["update_exit"] if restricted else state["full_update_exit"]
        if code == 0 and restricted:
            _write_list(state["repo"])
        return code
    if args[0] == "indextargets":
        print(LIST)
        return 0
    if args[0] == "install":
        if state["install_failures"] > 0:
            state["install_failures"] -= 1
            print("E: Unable to locate a dependency", file=sys.stderr)
            return 100
        for package in (arg for arg in args[1:] if not arg.startswith("-")):
            if "=" in package or package.startswith("DPkg::"):
                continue
            state["installed"][package] = ["ii ", _newest(state["repo"][package])]
        return 0
    raise SystemExit(f"stub apt-get: unexpected {args}")


def apt_helper(state: dict, args: list[str]) -> int:
    print(Path(args[1]).read_text(), end="")
    return 0


def systemctl(state: dict, args: list[str]) -> int:
    if args != ["start", "homelab-reconcile.service"]:
        raise SystemExit(f"stub systemctl: unexpected {args}")
    record = json.loads(RECORD.read_text()) if RECORD.exists() else {}
    run = {
        "versions": {pkg: version for pkg, (_, version) in state["installed"].items()},
        "ok": state["reconcile"] == "ok",
        "finished": int(state["now"]),
    }
    record["result"] = run
    if run["ok"]:
        record["last_ok"] = run
    RECORD.parent.mkdir(parents=True, exist_ok=True)
    RECORD.write_text(json.dumps(record))
    return 0 if run["ok"] else 1


def _write_list(repo: dict | None) -> None:
    if repo is None:
        LIST.unlink(missing_ok=True)
        return
    LIST.parent.mkdir(parents=True, exist_ok=True)
    stanzas = (
        f"Package: {package}\nVersion: {version}\nArchitecture: all\n"
        for package, versions in repo.items()
        for version in versions
    )
    LIST.write_text("\n".join(stanzas))


def _newest(versions: list[str]) -> str:
    best = versions[0]
    for version in versions[1:]:
        newer = ["/usr/bin/dpkg", "--compare-versions", version, "gt", best]
        if subprocess.run(newer, check=False).returncode == 0:
            best = version
    return best


HANDLERS = {
    "hostname": hostname,
    "date": date,
    "dpkg-query": dpkg_query,
    "dpkg": dpkg,
    "apt-get": apt_get,
    "apt-helper": apt_helper,
    "systemctl": systemctl,
}

if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2:]))
