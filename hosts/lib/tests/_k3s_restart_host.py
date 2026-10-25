"""A fake daniel-box for `homelab-k3s-restart`: a root directory, a stub systemctl and a stub API.

`Box.run()` runs the script in-process against `tmp_path` as HOMELAB_ROOT. Time is fake: the
script's `sleep` advances `Box.now`, so a 300-second poll costs nothing. `Box.routes` is the
cluster `k3s_gates.py host` reads (a copy of `healthy()`), and a test overrides one route to make
exactly one check refuse.
"""

import hashlib
import importlib.machinery
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from subprocess import CompletedProcess

from _k3s_gates_fixtures import NOW, fake_get, healthy, node

import k3s_gates

SCRIPT = Path(k3s_gates.__file__).with_name("homelab-k3s-restart")
UNIT = "k3s.service"
CONFIG = "/etc/rancher/k3s/config.yaml.d/50-homelab.yaml"


def load():
    loader = importlib.machinery.SourceFileLoader("homelab_k3s_restart", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def stamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Box:
    def __init__(self, root: Path, monkeypatch) -> None:
        self.root = root
        self.mod = load()
        self.now = NOW.timestamp()
        self.routes = healthy()
        self.calls: list[tuple[str, ...]] = []
        self.pid = "100"
        self.pid_after_restart = "200"
        self.active = "active"
        self.beats_after_restart = True
        self.restarted_at: float | None = None
        for rel in (
            "usr/lib/homelab",
            "var/lib/node-exporter-textfile",
            "run/lock",
            "var/lib/gitops-deploy",
        ):
            (root / rel).mkdir(parents=True)
        (root / "run/lock/server-deploy-all.lock").touch()
        (root / "var/lock").symlink_to("/run/lock")
        self.write_config("a: 1\n")
        monkeypatch.setattr(self.mod, "ROOT", root)
        monkeypatch.setattr(self.mod, "clock", lambda: self.now)
        monkeypatch.setattr(self.mod, "sleep", self._sleep)
        monkeypatch.setattr(self.mod, "systemctl", self._systemctl)
        monkeypatch.setattr(
            self.mod, "cluster_client_factory", lambda *a, **k: self._get
        )

    # ── the host ────────────────────────────────────────────────────────────────────────────

    def write_config(self, text: str) -> None:
        target = self.root / CONFIG.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        manifest = {"activations": {CONFIG: f"manual:{UNIT}", "/etc/x": "none"}}
        (self.root / self.mod.MANIFEST).write_text(json.dumps(manifest))

    def state(self, name: str) -> str | None:
        path = self.root / self.mod.STATE_DIR / name
        return path.read_text().strip() if path.exists() else None

    def set_state(self, name: str, text: str) -> None:
        directory = self.root / self.mod.STATE_DIR
        directory.mkdir(parents=True, exist_ok=True)
        (directory / name).write_text(text + "\n")

    def hash(self) -> str:
        line = f"{CONFIG} {hashlib.sha256((self.root / CONFIG.lstrip('/')).read_bytes()).hexdigest()}"
        return hashlib.sha256(line.encode()).hexdigest()

    def metrics(self) -> dict[str, float]:
        text = (
            self.root / "var/lib/node-exporter-textfile/homelab_k3s_restart.prom"
        ).read_text()
        return {
            line.split("{")[0].removeprefix("homelab_k3s_restart_"): float(
                line.split()[-1]
            )
            for line in text.splitlines()
        }

    # ── the stubs ───────────────────────────────────────────────────────────────────────────

    def _sleep(self, seconds: float) -> None:
        self.now += seconds

    def _systemctl(self, *args: str) -> CompletedProcess:
        self.calls.append(args)
        out = ""
        if args[:1] == ("--no-block",):
            self.restarted_at = self.now
            self.pid = self.pid_after_restart
        elif args[0] == "show":
            out = self.pid
        elif args[0] == "is-active":
            out = self.active
        return CompletedProcess(args, 0, out + "\n", "")

    def _get(self, path: str) -> dict:
        if path == f"{k3s_gates.NODES_PATH}/daniel-box":
            fresh = self.beats_after_restart and self.restarted_at is not None
            doc = node("daniel-box", server=True)
            beat = self.now if fresh else NOW.timestamp() - 3600
            doc["status"]["conditions"][0]["lastHeartbeatTime"] = stamp(beat)
            return doc
        return fake_get(self.routes)(path)

    # ── a run ───────────────────────────────────────────────────────────────────────────────

    def run(self, *flags: str) -> int:
        argv = [
            "--host",
            "daniel-box",
            "--unit",
            UNIT,
            "--token-file",
            "unused",
            *flags,
        ]
        return self.mod.main(argv)

    def restarts(self) -> int:
        return sum(1 for call in self.calls if call[:2] == ("--no-block", "restart"))
