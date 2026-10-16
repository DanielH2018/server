"""A fake daniel-pi for `hosts/lib/homelab-reconcile`: stub state, a manifest, and a runner.

`_reconcile_stub.py` documents the stub's state. Reconcile runs under this interpreter, which
is newer than the 3.12 the hosts run; `test_reconcile_component.py` holds its syntax to 3.12.
"""

import json
import shlex
import subprocess
import sys
from pathlib import Path

from lib.proc_testing import run
from lib.repo_paths import REPO

RECONCILE = REPO / "hosts" / "lib" / "homelab-reconcile"
STUB = Path(__file__).with_name("_reconcile_stub.py")
COMMANDS = ("hostname", "dpkg-query", "dpkg", "crontab", "systemctl")
# The verbs that change a unit, as opposed to the reads reconcile asks before acting.
ACTIONS = frozenset({"start", "stop", "restart", "try-restart", "disable"})

V1 = "0.100+gaaaaaaaaaaaa"
UNIT_DIR = "usr/lib/systemd/system"


class Node:
    """A fake daniel-pi: the stub's state, the files reconcile reads, and a way to run it."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "root"
        self.stub = tmp_path / "stub"
        self.bin = tmp_path / "bin"
        # A test drops /usr/bin from this to make a host binary absent, not merely stubbed.
        self.path = f"{self.bin}:/usr/bin:/bin"
        for directory in (self.root, self.stub, self.bin):
            directory.mkdir()
        for command in COMMANDS:
            wrapper = self.bin / command
            wrapper.write_text(
                f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(STUB))} "
                f'{command} "$@"\n'
            )
            wrapper.chmod(0o755)
        self.lock = self.root / "var/lib/dpkg/lock-frontend"
        self.lock.parent.mkdir(parents=True)
        self.lock.touch()
        self.applied_path = self.root / "var/lib/homelab-reconcile/applied.json"
        self.manifest_path = self.root / "usr/lib/homelab/manifest.json"
        self.state: dict = {
            "host": "daniel-pi",
            "installed": {
                "homelab-updater": ["ii ", V1],
                "homelab-daniel-pi": ["ii ", V1],
            },
            "units": {},
            "fail": [],
            "owned": [],
            "crontabs": {},
        }
        self.manifest: dict = {
            "units": {},
            "enable_links": [],
            "legacy": [],
            "reconcilers": [],
        }

    def unit(
        self,
        name: str,
        text: str = "[Unit]\n",
        *,
        active: str = "inactive",
        load: str = "loaded",
        enabled: str = "disabled",
        linked: bool = False,
        policy: str | None = None,
    ) -> None:
        """Ship unit `name`: its file, its manifest entry, its enable link and its live state."""
        self.write_unit(name, text)
        self.manifest["units"][name] = {"policy": policy} if policy else {}
        if linked:
            self.manifest["enable_links"].append(name)
        self.state["units"][name] = {"load": load, "active": active, "enabled": enabled}

    def write_unit(self, name: str, text: str) -> None:
        path = self.root / UNIT_DIR / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def write_dropin(self, name: str, text: str) -> None:
        path = self.root / UNIT_DIR / f"{name}.d" / "50-homelab.conf"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def drop(self, name: str) -> None:
        """Remove unit `name` from the package, as an upgrade that drops it would."""
        del self.manifest["units"][name]
        if name in self.manifest["enable_links"]:
            self.manifest["enable_links"].remove(name)
        (self.root / UNIT_DIR / name).unlink()

    def unlink(self, name: str) -> None:
        self.manifest["enable_links"].remove(name)

    def _prepare(self) -> dict[str, str]:
        (self.stub / "state.json").write_text(json.dumps(self.state))
        (self.stub / "calls.jsonl").write_text("")
        self.write_manifest()
        return {
            "PATH": self.path,
            "HOMELAB_ROOT": str(self.root),
            "STUB_STATE": str(self.stub),
        }

    def write_manifest(self) -> None:
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_text(json.dumps(self.manifest))

    def run(self) -> subprocess.CompletedProcess:
        result = run([sys.executable, str(RECONCILE)], env=self._prepare())
        self.state = json.loads((self.stub / "state.json").read_text())
        return result

    def start(self) -> subprocess.Popen:
        """Start reconcile without waiting for it; `finish` collects it."""
        return subprocess.Popen(
            [sys.executable, str(RECONCILE)],
            env=self._prepare(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def finish(self, process: subprocess.Popen) -> int:
        process.communicate(timeout=60)
        self.state = json.loads((self.stub / "state.json").read_text())
        return process.returncode

    def calls(self, command: str | None = None) -> list[dict]:
        lines = (self.stub / "calls.jsonl").read_text().splitlines()
        calls = [json.loads(line) for line in lines]
        return [call for call in calls if command is None or call["argv"][0] == command]

    def actions(self) -> list[list[str]]:
        """Each systemctl call that changes a unit, as [verb, unit]."""
        return [
            call["argv"][1:]
            for call in self.calls("systemctl")
            if call["argv"][1] in ACTIONS
        ]

    def applied(self) -> dict:
        return json.loads(self.applied_path.read_text())
