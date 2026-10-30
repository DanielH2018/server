#!/usr/bin/env python3
"""A booted test container for the host_packages container cases, driven with `docker exec`.

`scripts/validate/host_package_cases.py` runs the cases through it. The container runs the
test image with systemd as PID 1 and hostname daniel-pi unless told otherwise, and serves the repository
`host_fixtures.publish` writes with `python3 -m http.server` on 127.0.0.1:8791.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import shlex
import subprocess
import time
import sys
from pathlib import Path

from validate.validate_lib.host_fixtures import HOST, HOST_PKG, UPDATER

PORT = 8791
SERVER_LOG = "/var/log/test-repo.log"
PROM = "/var/lib/node-exporter-textfile/homelab_host.prom"
LAST_SUCCESS = "/var/lib/homelab-updater/last_success"
HOLD = "/var/lib/homelab-updater/hold"


# apt fetches the signed InRelease first, and Release only when that is missing.
RELEASE_GETS = tuple(
    f'"GET /dists/homelab/{name} HTTP/' for name in ("InRelease", "Release")
)


def release_fetched(log: str) -> bool:
    """Whether an http.server log holds a GET of the suite's InRelease or Release file."""
    return any(get in line for line in log.splitlines() for get in RELEASE_GETS)


def list_problems(names: list[str], box: str) -> list[str]:
    """What is wrong with the names in `/var/lib/apt/lists/`.

    A list named after `box`, daniel-box's address, means a fetch read the shipped
    homelab.sources rather than the test's. No list for the test repository means the check
    above looked at nothing.
    """
    problems = [
        f"/var/lib/apt/lists/{name} is named after daniel-box's address {box}"
        for name in names
        if name.split("_", 1)[0].split(":", 1)[0] == box
    ]
    if not any(name.startswith(f"127.0.0.1:{PORT}_") for name in names):
        problems.append(f"/var/lib/apt/lists/ holds no list for 127.0.0.1:{PORT}")
    return problems


def success_timestamp(prom: str) -> str:
    """The value of the success-timestamp series in a homelab_host.prom, or ""."""
    for line in prom.splitlines():
        if line.startswith("homelab_host_update_success_timestamp_seconds{"):
            return line.rsplit(" ", 1)[1]
    return ""


class CaseFailed(Exception):
    """A case's assertion that did not hold; the message says what was found instead."""


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise CaseFailed(message)


class Container:
    """One booted test container, driven with `docker exec`."""

    def __init__(
        self,
        image: str,
        name: str,
        work: Path,
        hostname: str = HOST,
        mounts: tuple[str, ...] = (),
        network: str | None = None,
    ) -> None:
        """Name a container that `boot` starts.

        Args:
            image: The test image's tag.
            name: The container's name.
            work: The cases' scratch directory, whose `repo` and `debs` the container mounts.
            hostname: The container's hostname, which bootstrap-host.sh's step 1 reads.
            mounts: Further `docker run -v` arguments.
            network: A `docker run --network` value, such as `none`; None keeps Docker's default.
        """
        self.image = image
        self.name = name
        self.work = work
        self.hostname = hostname
        self.mounts = mounts
        self.network = network

    def boot(self, python: str) -> None:
        """Start the container and wait for systemd to finish starting up.

        Raises:
            CaseFailed: systemd never finished, or python3 is not the leg's minor.
        """
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True)
        subprocess.run(
            ["docker", "run", "-d", "--name", self.name, "--privileged"]
            + ["--cgroupns=host", "--hostname", self.hostname]
            + (["--network", self.network] if self.network else [])
            + [arg for mount in self.mounts for arg in ("-v", mount)]
            + ["-v", f"{self.work / 'repo'}:/srv/repo:ro"]
            + ["-v", f"{self.work / 'debs'}:/srv/debs:ro", self.image],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        # A docker exec that lands before PID 1 has its private socket up gets no answer at
        # all, so the wait itself is retried until systemd answers.
        deadline = time.monotonic() + 180
        while True:
            result = self.sh("timeout 120 systemctl is-system-running --wait")
            state = result.stdout.strip()
            if state or time.monotonic() > deadline:
                break
            time.sleep(1)
        if state not in ("running", "degraded"):
            logs = subprocess.run(
                ["docker", "logs", "--tail", "60", self.name],
                capture_output=True,
                text=True,
                check=False,
            )
            print(
                f"{self.name}: systemctl said {result.stderr.strip()!r}; the container's"
                f" log:\n{logs.stdout}{logs.stderr}",
                file=sys.stderr,
            )
        if state == "degraded":
            failed = self.sh("systemctl --failed --no-legend --plain").stdout
            print(f"{self.name}: systemd is degraded:\n{failed}", file=sys.stderr)
        expect(state in ("running", "degraded"), f"systemd reached {state!r}")
        version = self.sh("python3 --version").stdout.strip()
        expect(
            version.startswith(f"Python {python}."),
            f"python3 --version printed {version!r}, not Python {python}",
        )

    def sh(self, script: str, timeout: int = 900) -> subprocess.CompletedProcess:
        """Run `script` with sh inside the container. A non-zero exit is not an error."""
        return subprocess.run(
            ["docker", "exec", self.name, "sh", "-c", script],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def ok(self, script: str) -> str:
        """Run `script`, and return its stdout.

        Raises:
            CaseFailed: The script exited non-zero.
        """
        result = self.sh(script)
        expect(
            result.returncode == 0,
            f"{script!r} exited {result.returncode}: {result.stderr.strip()}",
        )
        return result.stdout

    def remove(self) -> None:
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True)

    def serve(self) -> None:
        """Start the test repository's server as a transient unit, and wait for it."""
        command = shlex.join(
            ["python3", "-u", "-m", "http.server", "--bind", "127.0.0.1"]
            + ["--directory", "/srv/repo", str(PORT)]
        )
        self.ok(
            "systemd-run --unit=test-repo"
            f" -p StandardOutput=append:{SERVER_LOG} -p StandardError=append:{SERVER_LOG}"
            f" {command}"
        )
        self.ok(
            "for i in $(seq 100); do"
            f" python3 -c 'import urllib.request; urllib.request.urlopen(\"http://127.0.0.1:{PORT}/\")'"
            " 2>/dev/null && exit 0; sleep 0.1; done; exit 1"
        )

    def stop_serving(self) -> None:
        self.ok("systemctl stop test-repo.service")

    def log_size(self) -> int:
        return int(self.ok(f"touch {SERVER_LOG}; stat -c %s {SERVER_LOG}"))

    def log_since(self, offset: int) -> str:
        return self.ok(f"tail -c +{offset + 1} {SERVER_LOG}")

    def run_updater(self) -> int:
        """Run homelab-updater.service to its end; return the updater's exit status."""
        self.sh(f"systemctl start {UPDATER}.service")
        return int(self.ok(f"systemctl show -P ExecMainStatus {UPDATER}.service"))

    def bootstrap(self, deb: str) -> int:
        """Run the updater a deb in /srv/debs ships, on a host with none installed."""
        result = self.sh(
            f"rm -rf /tmp/bootstrap && dpkg-deb -x /srv/debs/{deb} /tmp/bootstrap"
            " && /tmp/bootstrap/usr/lib/homelab/homelab-updater"
        )
        if result.returncode:
            print(result.stderr, file=sys.stderr)
        return result.returncode

    def installed(self) -> dict[str, str]:
        """Each of the two homelab packages and tree that dpkg holds as `ii `, by version."""
        out = self.sh(
            "dpkg-query -W -f '${Package} ${db:Status-Abbrev}${Version}\\n'"
            f" {UPDATER} {HOST_PKG} tree"
        ).stdout
        return {
            package: rest[3:]
            for package, rest in (line.split(" ", 1) for line in out.splitlines())
            if rest.startswith("ii ")
        }

    def active(self, unit: str) -> str:
        return self.ok(f"systemctl show -P ActiveState {unit}").strip()

    def exists(self, path: str) -> bool:
        return self.sh(f"test -e {path}").returncode == 0

    def read(self, path: str) -> str:
        """The file's content, or "" when it does not exist."""
        return self.sh(f"cat {path}").stdout

    def written(self) -> tuple[str, ...]:
        """What the updater writes: the metrics file with its mtime, last_success, the hold."""
        return (
            self.read(PROM),
            self.sh(f"stat -c %y {PROM}").stdout,
            self.read(LAST_SUCCESS),
            str(self.exists(HOLD)),
        )


TOOLS_BIN = "/usr/lib/homelab/bin"
# Whether each homelab-tools binary is on PATH. homelab-tools keeps every one off it, and the
# host packages' sops component puts sops back as an exec of the packaged binary.
TOOLS_ON_PATH = {"sops": True, "chezmoi": False, "nfpm": False}


def expect_tools_run(c: Container) -> None:
    """Each packaged tool runs, and `sops` alone resolves on PATH, to the packaged binary.

    Raises:
        CaseFailed: A tool does not run, `sops` on PATH prints another version, or another
            tool is on PATH.
    """
    for tool, on_path in TOOLS_ON_PATH.items():
        packaged = c.ok(f"{TOOLS_BIN}/{tool} --version")
        found = c.sh(f"{tool} --version")
        if on_path:
            expect(
                found.stdout.split()[:2] == packaged.split()[:2],
                f"{tool} on PATH is not {TOOLS_BIN}/{tool}",
            )
        else:
            expect(
                found.returncode != 0,
                f"{tool} is on PATH; it installs at {TOOLS_BIN} only",
            )
