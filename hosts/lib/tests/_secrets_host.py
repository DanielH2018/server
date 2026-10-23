"""The `Host` fixture class the `homelab-secrets` suites share: a temporary tree and a stub `sops`."""

import json
import stat
import types
from typing import Any

from lib.repo_paths import REPO

SCRIPT = REPO / "hosts" / "lib" / "homelab-secrets"
SOPS_FILE = "/usr/lib/homelab/secrets/daniel-server.sops.yaml"
TEMPLATES = "/usr/lib/homelab/secret-templates"
STUB_SOPS = """#!/bin/sh
[ "$1 $2 $3" = "--decrypt --output-type json" ] || exit 2
[ "$4" = "{ciphertext}" ] || exit 3
echo called >> "{log}"
[ -f "{plain}" ] || {{ echo "no age key" >&2; exit 1; }}
cat "{plain}"
"""


class Host:
    """A temporary tree the script reads and writes, with a stub `sops` and a recorded chown."""

    def __init__(self, tmp_path, monkeypatch):
        self.root = tmp_path / "root"
        self.plain = tmp_path / "plain.json"
        self.log = tmp_path / "sops.log"
        self.chowns: list[tuple[str, str, str]] = []
        self.credentials: list[str] = []
        self.outputs: list[dict] = []
        monkeypatch.setenv("HOMELAB_ROOT", str(self.root))
        self.module: Any = types.ModuleType("homelab_secrets")
        exec(compile(SCRIPT.read_text(), str(SCRIPT), "exec"), self.module.__dict__)
        self.module._chown = lambda *args: self.chowns.append(args)
        self.sops = self.path("/usr/lib/homelab/bin/sops")
        self.sops.parent.mkdir(parents=True)
        ciphertext = f"{self.root}{SOPS_FILE}"
        self.sops.write_text(
            STUB_SOPS.format(ciphertext=ciphertext, log=self.log, plain=self.plain)
        )
        self.sops.chmod(0o755)

    def path(self, absolute):
        return self.root / absolute.lstrip("/")

    def decrypts_to(self, **values):
        self.plain.write_text(json.dumps(values))

    def output(self, destination, template, *, persist=False, keys=(), **fields):
        source = self.path(f"{TEMPLATES}/{destination}")
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(template)
        self.outputs.append(
            {
                "template": f"{TEMPLATES}/{destination}",
                "destination": destination,
                "owner": "root",
                "group": "nut",
                "mode": "0640",
                "persist": persist,
                "dependents": ["nut-monitor.service"],
                "keys": list(keys),
            }
            | fields
        )

    def run(self):
        manifest = self.path("/usr/lib/homelab/secrets.json")
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            json.dumps(
                {
                    "sops_file": SOPS_FILE,
                    "credentials": self.credentials,
                    "outputs": self.outputs,
                }
            )
        )
        return self.module.main([])

    def read(self, absolute):
        return self.path(absolute).read_text()

    def mode(self, absolute):
        return stat.S_IMODE(self.path(absolute).stat().st_mode)

    def decrypts(self):
        return self.log.read_text().count("called") if self.log.exists() else 0
