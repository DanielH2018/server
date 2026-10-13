"""A fake daniel-pi for `hosts/lib/homelab-updater`: stub state, state paths, and a runner.

`_updater_stub.py` documents the stub's state. The updater runs under `/bin/sh`, which is dash
on Ubuntu, so a bashism fails the suites that use this.
"""

import json
import shlex
import sys
from pathlib import Path

from lib.proc_testing import run
from lib.repo_paths import REPO

UPDATER = REPO / "hosts" / "lib" / "homelab-updater"
STUB = Path(__file__).with_name("_updater_stub.py")
COMMANDS = (
    "hostname",
    "date",
    "dpkg-query",
    "dpkg",
    "apt-get",
    "apt-helper",
    "systemctl",
)

V1 = "0.100+gaaaaaaaaaaaa"
V2 = "0.101+gbbbbbbbbbbbb"
NOW = "1760000000"
EARLIER = "1750000000"


class Host:
    """A fake daniel-pi: the stub's state, the updater's state paths, and a way to run it."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "root"
        self.stub = tmp_path / "stub"
        self.bin = tmp_path / "bin"
        for directory in (self.root, self.stub, self.bin):
            directory.mkdir()
        for command in COMMANDS:
            wrapper = self.bin / command
            wrapper.write_text(
                f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(STUB))} "
                f'{command} "$@"\n'
            )
            wrapper.chmod(0o755)
        self.textfile = self.root / "var/lib/node-exporter-textfile"
        self.textfile.mkdir(parents=True)
        self.state_dir = self.root / "var/lib/homelab-updater"
        self.hold = self.state_dir / "hold"
        self.last_success = self.state_dir / "last_success"
        self.record = self.root / "var/lib/homelab-reconcile/applied.json"
        self.prom = self.textfile / "homelab_host.prom"
        self.state = {
            "host": "daniel-pi",
            "installed": {
                "homelab-updater": ["ii ", V1],
                "homelab-daniel-pi": ["ii ", V1],
            },
            "repo": {"homelab-updater": [V1], "homelab-daniel-pi": [V1]},
            "update_exit": 0,
            "full_update_exit": 0,
            "install_failures": 0,
            "install_output": "E: Unable to correct problems, you have held broken packages.",
            "audit": "",
            "configure_exit": 0,
            "configure_output": "",
            "reconcile": "ok",
            "verify": "",
            "hour": "12",
            "today": "2026-10-13",
            "now": NOW,
        }
        self.reconciled_at(V1)

    def reconciled_at(
        self, version: str, ok: bool = True, earlier_ok: str = V1
    ) -> None:
        """Write a record whose result names `version` for both packages."""
        result = {
            "versions": {"homelab-updater": version, "homelab-daniel-pi": version},
            "ok": ok,
            "finished": 1,
        }
        last_ok = (
            result
            if ok
            else {
                **result,
                "versions": dict.fromkeys(result["versions"], earlier_ok),
                "ok": True,
            }
        )
        self.record.parent.mkdir(parents=True, exist_ok=True)
        self.record.write_text(json.dumps({"result": result, "last_ok": last_ok}))

    def succeeded_before(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.last_success.write_text(f"{EARLIER}\n")

    def run(self, *args: str):
        (self.stub / "state.json").write_text(json.dumps(self.state))
        (self.stub / "calls.jsonl").write_text("")
        env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOMELAB_ROOT": str(self.root),
            "STUB_STATE": str(self.stub),
        }
        result = run(["/bin/sh", str(UPDATER), *args], env=env)
        self.state = json.loads((self.stub / "state.json").read_text())
        return result

    def calls(self, command: str | None = None) -> list[list[str]]:
        lines = (self.stub / "calls.jsonl").read_text().splitlines()
        calls = [json.loads(line) for line in lines]
        return [call for call in calls if command is None or call[0] == command]

    def installs(self) -> list[list[str]]:
        return [call for call in self.calls("apt-get") if call[1] == "install"]

    def reconciles(self) -> int:
        return len(self.calls("systemctl"))

    def metrics(self) -> dict[str, str]:
        """Each series in `homelab_host.prom`, by name, mapped to its labels and value."""
        series = {}
        for line in self.prom.read_text().splitlines():
            name, _, rest = line.partition("{")
            series[name] = "{" + rest
        return series

    def publish(self, version: str) -> None:
        self.state["repo"] = {
            "homelab-updater": [V1, version],
            "homelab-daniel-pi": [V1, version],
        }


def reconcile_ok(metrics: dict[str, str]) -> str:
    """The value of `homelab_host_reconcile_ok` in `Host.metrics()`."""
    return metrics["homelab_host_reconcile_ok"].rsplit(" ", 1)[1]
