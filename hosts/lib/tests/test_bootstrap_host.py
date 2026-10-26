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
from _bootstrap_host import (
    AGE_KEY,
    INSTALLED_KEYRING,
    KEYRING,
    SOURCES,
    FakeHost,
)


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
    update, tools, install = host.apt_calls()
    assert update.startswith("update --error-on=any ")
    assert "-o Dir::Etc::sourcelist=sources.list.d/homelab.sources" in update
    assert "-o Dir::Etc::sourceparts=-" in update
    assert "-o APT::Get::List-Cleanup=0" in update
    assert tools == "install -y homelab-tools"
    assert install == "install -y homelab-updater homelab-daniel-pi"
    assert "tmpfiles --create" in host.commands()


def test_another_host_is_refused_before_anything_is_written(host):
    result = host.run("daniel-server")

    assert result.returncode == 1
    assert "this host is daniel-pi, not daniel-server" in result.stderr
    assert host.written() == []
    assert host.apt_calls() == []


def test_a_host_the_script_does_not_know_is_refused_before_anything_is_written(
    tmp_path,
):
    host = FakeHost(tmp_path, hostname="daniel-laptop")

    result = host.run("daniel-laptop")

    assert result.returncode == 1
    assert "not a host this script knows" in result.stderr
    assert host.written() == []
    assert host.apt_calls() == []


def test_a_missing_keyring_is_refused_before_anything_is_written(host):
    host.keyring.unlink()

    result = host.run("daniel-pi")

    assert result.returncode == 1
    assert "cannot read" in result.stderr
    assert host.written() == []
    assert host.apt_calls() == []


def test_until_stops_after_the_named_step(host):
    result = host.run("daniel-pi", "--until", "3")

    assert result.returncode == 0, result.stderr
    assert (host.root / AGE_KEY).exists()
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


def _public_line(result: subprocess.CompletedProcess) -> list[str]:
    return [line for line in result.stdout.splitlines() if "age public key for" in line]


def test_a_missing_age_key_is_created_private_and_only_its_public_half_printed(host):
    result = host.run("daniel-pi")

    assert result.returncode == 0, result.stderr
    key = host.root / AGE_KEY
    assert key.stat().st_mode & 0o777 == 0o600
    [line] = _public_line(result)
    assert line.split(": ")[-1].startswith("age1")
    secret = next(x for x in key.read_text().splitlines() if x.startswith("AGE-SECRET"))
    assert secret not in result.stdout + result.stderr
    assert not (host.root / (AGE_KEY + ".tmp")).exists()


def test_an_existing_age_key_is_kept_and_its_mode_repaired(host):
    first = host.run("daniel-pi")
    key = host.root / AGE_KEY
    before = key.read_bytes()
    key.chmod(0o640)

    second = host.run("daniel-pi")

    assert second.returncode == 0, second.stderr
    assert key.read_bytes() == before
    assert key.stat().st_mode & 0o777 == 0o600
    assert _public_line(second) == _public_line(first)


def test_a_host_without_age_keygen_and_without_a_key_is_refused(host):
    # The scratch PATH holds only the stubs, and gpg and the coreutils the script needs are
    # linked in, so age-keygen is the one command missing.
    for tool in (
        "gpg",
        "awk",
        "install",
        "mkdir",
        "cat",
        "chmod",
        "mv",
        "rm",
        "dirname",
        "mktemp",
    ):
        found = shutil.which(tool)
        assert found
        (host.bin / tool).symlink_to(found)
    result = subprocess.run(
        ["/bin/sh", str(host.dir / "bootstrap-host.sh"), "daniel-pi"],
        env={"PATH": str(host.bin), "HOMELAB_ROOT": str(host.root)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 1
    assert "age-keygen is not installed" in result.stderr
    assert not (host.root / AGE_KEY).exists()
