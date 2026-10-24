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
COMMANDS = (
    "hostname",
    "dpkg-query",
    "dpkg",
    "crontab",
    "systemctl",
    "sysctl",
    "sshd",
    "fail2ban-client",
    "ufw",
    "getent",
    "usermod",
    "systemd-sysusers",
    "virsh",
)
# The verbs that change a unit, as opposed to the reads reconcile asks before acting.
ACTIONS = frozenset({"start", "stop", "restart", "try-restart", "disable"})

# Every systemctl verb that changes a unit or its failed state, `reset-failed` included.
WRITES = ACTIONS | {"reset-failed"}
SECRETS_UNIT = "homelab-secrets.service"

V1 = "0.100+gaaaaaaaaaaaa"
UNIT_DIR = "usr/lib/systemd/system"


class Node:
    """A fake daniel-pi: the stub's state, the files reconcile reads, and a way to run it."""

    def __init__(self, tmp_path: Path, script: Path = RECONCILE) -> None:
        # A red fixture passes a copy of reconcile with one rule removed.
        self.script = script
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
            "commands": {},
            "ufw": {"active": True, "rules": [], "fail": []},
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
        result = run([sys.executable, str(self.script)], env=self._prepare())
        self.state = json.loads((self.stub / "state.json").read_text())
        return result

    def command(self) -> tuple[list[str], dict[str, str]]:
        """The argv and environment that run reconcile on the current state, for a caller that starts it."""
        return [sys.executable, str(self.script)], self._prepare()

    def start(self) -> subprocess.Popen:
        """Start reconcile without waiting for it; `finish` collects it."""
        return subprocess.Popen(
            [sys.executable, str(self.script)],
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

    def writes(self) -> list[list[str]]:
        """Each systemctl call that changes a unit or clears its failure, as [verb, unit]."""
        return [
            call["argv"][1:]
            for call in self.calls("systemctl")
            if call["argv"][1] in WRITES
        ]

    def secrets(
        self,
        outputs: list[dict] | None = None,
        credentials: list[str] | None = None,
        digest: str = "a" * 64,
    ) -> None:
        """Make this a package that ships `secrets-unit`: the digest, secrets.json and the unit."""
        self.manifest["secrets_inputs_sha256"] = digest
        self.unit(SECRETS_UNIT, "[Service]\nType=oneshot\n", active="active")
        path = self.root / "usr/lib/homelab/secrets.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"credentials": credentials or [], "outputs": outputs or []})
        )

    def output_file(self, destination: str, *, persist: bool = False) -> None:
        """Write the rendered output `homelab-secrets` would have left at `destination`."""
        top = "var/lib/homelab-secrets" if persist else "run/homelab-secrets"
        path = self.root / top / destination
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("rendered\n")

    def credential(self, key: str) -> None:
        path = self.root / "run/credstore" / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("value\n")

    @property
    def pending_path(self) -> Path:
        return self.root / "run/homelab-secrets/pending.json"

    def queue(self, *units: str) -> None:
        self.pending_path.parent.mkdir(parents=True, exist_ok=True)
        self.pending_path.write_text(json.dumps(sorted(units)))

    def pending(self) -> list[str]:
        return json.loads(self.pending_path.read_text())

    def applied(self) -> dict:
        return json.loads(self.applied_path.read_text())
