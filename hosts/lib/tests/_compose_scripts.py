"""A scratch Pi for the compose-run and compose-adopt tests: a stub `docker` that logs its argv.

`Pi` lays out the files the scripts read under a scratch `HOMELAB_ROOT`, puts stubs for `docker`,
`dpkg-query` and `logger` first on PATH, and runs a script against them. The stub `docker` answers
from files in its state directory, so a test sets up a case by writing one file.

The `*_problems` functions are the checks each test applies. A test applies each to the real
script, which must pass, and to a fixture script that breaks the rule, which must be flagged.
"""

import os
import re
import subprocess
from pathlib import Path

from lib.repo_paths import REPO

COMPONENT = REPO / "hosts" / "components" / "pi-compose"
RUN = COMPONENT / "compose-run"
ADOPT = COMPONENT / "compose-adopt"
SECRET = "s3cret-value-Zq9"
DIGEST = "@sha256:" + "ab" * 32
VERSION = "0.412+g0123456789ab"

DOCKER = r"""#!/bin/sh
printf '%s\n' "$*" >> "$STUB/argv.log"
case "$*" in
  *"config --services"*) cat "$STUB/services" ;;
  *"config --images"*) cat "$STUB/images" ;;
  *"config -q"*) [ ! -e "$STUB/fail-config" ] ;;
  "compose -f "*" up -d"*) [ ! -e "$STUB/fail-up" ] ;;
  "image inspect "*) grep -qxF "$3" "$STUB/present" ;;
  "pull "*) [ ! -e "$STUB/fail-pull" ] ;;
  "ps -a "*)
    name=${4#name=^/}
    grep "^${name%\$}	" "$STUB/containers" || true ;;
  "rm -f "*) ;;
  *) echo "stub docker: unexpected $*" >&2; exit 99 ;;
esac
"""
DPKG_QUERY = '#!/bin/sh\nprintf "%s" "$(cat "$STUB/version")"\n'
LOGGER = '#!/bin/sh\nprintf "%s\\n" "$*" >> "$STUB/logger.log"\n'


class Pi:
    """The scratch Pi: its files, its stub state and a way to run a script."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "root"
        self.stub = tmp_path / "stub"
        self.bin = tmp_path / "bin"
        self.textfiles = tmp_path / "textfile"
        for directory in (self.stub, self.bin, self.textfiles):
            directory.mkdir()
        for name, body in (
            ("docker", DOCKER),
            ("dpkg-query", DPKG_QUERY),
            ("logger", LOGGER),
        ):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        self.compose = self.root / "usr/lib/homelab/compose"
        self.compose.mkdir(parents=True)
        (self.compose / "compose.yaml").write_text("name: homelab\ninclude: []\n")
        self.service("alloy", "domain", image="grafana/alloy:v1")
        self.env({"domain": "example.test"})
        self.containers: dict[str, str] = {}
        self.version(VERSION)
        self.images = {"grafana/alloy:v1" + DIGEST}
        self.present(*self.images)
        self.sync()

    def service(self, name: str, *keys: str, image: str) -> None:
        """Install a service's compose file, which reads each of `keys` as `${key}`."""
        path = self.compose / name / "compose.yaml"
        path.parent.mkdir(exist_ok=True)
        lines = [f"      - {key.upper()}=${{{key}}}" for key in keys]
        path.write_text(
            f"services:\n  {name}:\n    container_name: {name}\n"
            f"    image: {image}{DIGEST}\n    environment:\n" + "\n".join(lines) + "\n"
        )

    def env(self, values: dict[str, str]) -> None:
        text = "".join(f"{key}='{value}'\n" for key, value in values.items())
        (self.compose / "compose.env").write_text(text)

    def present(self, *images: str) -> None:
        (self.stub / "present").write_text("".join(f"{i}\n" for i in images))

    def version(self, version: str) -> None:
        (self.stub / "version").write_text(version)

    def fail(self, step: str) -> None:
        (self.stub / f"fail-{step}").touch()

    def sync(self) -> None:
        """Write the stub's answers from the installed project and `self.containers`."""
        services = sorted(p.parent.name for p in self.compose.glob("*/compose.yaml"))
        (self.stub / "services").write_text("".join(f"{s}\n" for s in services))
        (self.stub / "images").write_text(
            "".join(f"{i}\n" for i in sorted(self.images))
        )
        rows = (f"{name}\t{project}\n" for name, project in self.containers.items())
        (self.stub / "containers").write_text("".join(rows))
        (self.stub / "argv.log").touch()

    def run(self, script: Path = RUN, *args: str) -> subprocess.CompletedProcess:
        self.sync()
        env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOMELAB_ROOT": str(self.root),
            "TEXTFILE_DIR": str(self.textfiles),
            "STUB": str(self.stub),
            "HOMELAB_COMPOSE_WAIT_TIMEOUT": "730",
        }
        return subprocess.run(
            [str(script), *args], env=env, capture_output=True, text=True, check=False
        )

    def adopt(self, script: Path = ADOPT) -> subprocess.CompletedProcess:
        return self.run(script, str(self.compose / "compose.yaml"))

    @property
    def log(self) -> list[str]:
        return (self.stub / "argv.log").read_text().splitlines()

    @property
    def logger(self) -> list[str]:
        path = self.stub / "logger.log"
        return path.read_text().splitlines() if path.exists() else []

    @property
    def prom_path(self) -> Path:
        return self.textfiles / "homelab_compose.prom"

    def prom(self) -> dict[str, str]:
        """The metric file as `{series: value}`, with the label set kept in the series."""
        rows = [
            line.rsplit(" ", 1)
            for line in self.prom_path.read_text().splitlines()
            if not line.startswith("#")
        ]
        return dict(rows)


def mutate(script: Path, tmp_path: Path, old: str, new: str) -> Path:
    """A fixture copy of `script` with `old` replaced by `new`, beside a copy of compose-adopt."""
    text = script.read_text()
    assert old in text, f"{script.name} no longer holds {old!r}"
    directory = tmp_path / "fixture"
    directory.mkdir(exist_ok=True)
    fixture = directory / script.name
    fixture.write_text(text.replace(old, new))
    fixture.chmod(0o755)
    if script != ADOPT:
        (directory / "compose-adopt").write_text(ADOPT.read_text())
        (directory / "compose-adopt").chmod(0o755)
    return fixture


def refusal_problems(log: list[str]) -> list[str]:
    """Why a refused run touched a container or started the project."""
    return [
        f"refused run logged {line!r}"
        for line in log
        if line.startswith("rm -f") or " up -d" in line
    ]


def config_problems(log: list[str]) -> list[str]:
    """Why a `config` call could print the interpolated model: it names neither `-q`, `--services` nor `--images`."""
    return [
        f"bare config: {line!r}"
        for line in log
        if re.search(r"\bconfig\b", line)
        and not re.search(r"config (-q|--services|--images)$", line)
    ]


def sha_problems(prom: dict[str, str], expected: str) -> list[str]:
    """Why the result series does not carry the `sha` label `expected`."""
    series = [s for s in prom if s.startswith("homelab_compose_up_result{")]
    if len(series) != 1 or f'sha="{expected}"' not in series[0]:
        return [f"result series {series} lack sha={expected!r}"]
    return []


def mode_of(path: Path) -> int:
    return os.stat(path).st_mode & 0o777
