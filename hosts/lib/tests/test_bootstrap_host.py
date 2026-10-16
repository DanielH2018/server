"""`hosts/bootstrap-host.sh` on a fake host: its hostname check, its keyring check, its apt calls.

The script runs under stub `hostname` and `apt-get` commands and the real `gpg`, and writes under
HOMELAB_ROOT. `ansible/tests/repo/test_lan_facts_have_one_source.py` holds the script's URL and
fingerprint pin to their sources.

Run: uv run pytest hosts/lib/tests/test_bootstrap_host.py
"""

import shutil
import subprocess
from pathlib import Path

import pytest
from lib.repo_paths import REPO

SCRIPT = REPO / "hosts" / "bootstrap-host.sh"
KEYRING = REPO / "hosts" / "keys" / "homelab-archive-keyring.gpg"
INSTALLED_KEYRING = "usr/share/keyrings/homelab-archive-keyring.gpg"
SOURCES = "etc/apt/sources.list.d/homelab.sources"


class FakeHost:
    """A scratch directory holding the script, its keyring, stub commands and a HOMELAB_ROOT."""

    def __init__(self, tmp_path: Path, hostname: str = "daniel-pi") -> None:
        self.dir = tmp_path / "copy"
        self.dir.mkdir()
        shutil.copy(SCRIPT, self.dir / SCRIPT.name)
        self.keyring = self.dir / KEYRING.name
        shutil.copy(KEYRING, self.keyring)
        self.root = tmp_path / "root"
        self.root.mkdir()
        self.calls = tmp_path / "apt-get.calls"
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self._stub("hostname", f"echo {hostname}")
        self._stub("apt-get", f'echo "$*" >> {self.calls}')

    def _stub(self, name: str, body: str) -> None:
        stub = self.bin / name
        stub.write_text(f"#!/bin/sh\n{body}\n")
        stub.chmod(0o755)

    def run(self, *argv: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["/bin/sh", str(self.dir / SCRIPT.name), *argv],
            env={
                "PATH": f"{self.bin}:/usr/bin:/bin",
                "HOMELAB_ROOT": str(self.root),
            },
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def apt_calls(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def written(self) -> list[Path]:
        return [path for path in self.root.rglob("*") if path.is_file()]


@pytest.fixture
def host(tmp_path) -> FakeHost:
    return FakeHost(tmp_path)


@pytest.fixture(scope="module")
def other_keyring(tmp_path_factory) -> Path:
    """A keyring holding a key other than the archive key."""
    home = tmp_path_factory.mktemp("gnupg")
    home.chmod(0o700)
    gpg = ["gpg", "--homedir", str(home), "--batch", "--pinentry-mode", "loopback"]
    subprocess.run(
        [*gpg, "--passphrase", "", "--quick-generate-key", "t <t@example.invalid>"]
        + ["ed25519", "sign", "never"],
        check=True,
        capture_output=True,
        timeout=60,
    )
    keyring = home / "other.gpg"
    subprocess.run(
        [*gpg, "--output", str(keyring), "--export"],
        check=True,
        capture_output=True,
        timeout=60,
    )
    subprocess.run(
        ["gpgconf", "--homedir", str(home), "--kill", "gpg-agent"],
        check=True,
        capture_output=True,
        timeout=60,
    )
    return keyring


def test_the_named_host_gets_the_keyring_the_source_and_both_packages(host):
    result = host.run("daniel-pi")

    assert result.returncode == 0, result.stderr
    installed = host.root / INSTALLED_KEYRING
    assert installed.read_bytes() == KEYRING.read_bytes()
    assert installed.stat().st_mode & 0o777 == 0o644
    sources = (host.root / SOURCES).read_text().splitlines()
    # test_lan_facts_have_one_source.py holds the address itself.
    assert [line for line in sources if line.startswith("URIs: http://")]
    assert f"Signed-By: /{INSTALLED_KEYRING}" in sources
    update, install = host.apt_calls()
    assert update.startswith("update --error-on=any ")
    assert "-o Dir::Etc::sourcelist=sources.list.d/homelab.sources" in update
    assert "-o Dir::Etc::sourceparts=-" in update
    assert "-o APT::Get::List-Cleanup=0" in update
    assert install == "install -y homelab-updater homelab-daniel-pi"


def test_another_host_is_refused_before_anything_is_written(host):
    result = host.run("daniel-server")

    assert result.returncode == 1
    assert "this host is daniel-pi, not daniel-server" in result.stderr
    assert host.written() == []
    assert host.apt_calls() == []


def test_a_keyring_holding_another_key_is_refused(host, other_keyring):
    shutil.copy(other_keyring, host.keyring)

    result = host.run("daniel-pi")

    assert result.returncode == 1
    assert "not the archive key" in result.stderr
    assert host.written() == []
    assert host.apt_calls() == []


def test_a_failed_update_installs_nothing(host):
    host._stub("apt-get", f'echo "$*" >> {host.calls}; [ "$1" != update ]')

    result = host.run("daniel-pi")

    assert result.returncode == 1
    assert "apt-get update of the homelab source failed" in result.stderr
    assert len(host.apt_calls()) == 1
