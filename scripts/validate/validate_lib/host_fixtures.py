#!/usr/bin/env python3
"""The packages and the repository the host_packages container cases serve.

`scripts/validate/host_package_cases.py` runs the cases. This module builds what they install:
the fixture homelab-daniel-pi in three versions, rendered by the real renderer from
`hosts/lib/tests/container/fixture/`, a fourth, `sealed`, that carries `secrets-unit`
(H4.1c), and a fifth, `conffile`, whose line-arm edit lands in another package's conffile
(#6088), a sixth, `shadow`, whose unit an Ansible-era copy shadows (H7.41a); a homelab-updater whose unit changes; a copy of the real
homelab-daniel-pi whose drop-in on the updater's timer changes; the real homelab-daniel-pi
rendered with its container-recovery timer disarmed; and the apt repository, in
suite `homelab`, that serves a chosen set of them. `publish` signs that repository's `InRelease`
with a throwaway ed25519 key, generated once per process, and writes the key's public keyring
into the repository as TEST_KEYRING. The test image's source names that keyring in
`Signed-By:`, so apt checks the signature and the `Valid-Until` an expired-index case moves.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import atexit
import dataclasses
import functools
import hashlib
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta
from email.utils import formatdate
from pathlib import Path

from lib.estate import REPO_INVENTORY, Inventory
from lib.repo_paths import REPO
from renderer.renderer_lib import host as host_mode

FIXTURE = REPO / "hosts" / "lib" / "tests" / "container" / "fixture"
VARIANTS = ("base", "head", "payload", "sealed", "conffile", "shadow")
# The `sealed` variant carries `secrets-unit`, which ships a host's ciphertext. This stand-in is
# the file the render takes where no ciphertext is given, and it holds no value (H4.1c).
STAND_IN = FIXTURE.parent / "standin"
HOST = "daniel-pi"
UPDATER = "homelab-updater"
HOST_PKG = f"homelab-{HOST}"
PAYLOAD = "/usr/lib/homelab/fixture-payload"
PAYLOAD_BYTES = 64 * 1024 * 1024
KEYRING = "/usr/share/keyrings/homelab-archive-keyring.gpg"
UNIT_DIR = "/usr/lib/systemd/system"
UNIT_CHANGE = "Environment=HOMELAB_FIXTURE=unit-changed"
DROPIN = f"{UNIT_DIR}/{UPDATER}.timer.d/50-homelab.conf"
# The inventory flag whose false value drops the recovery timer's enable link (H3.8b).
RECOVERY_FLAG = "optimize_pi_recovery_restart_enabled"
RECOVERY_TIMER = "homelab-pi-recovery.timer"
# The real Pi drop-in's minute, and the one its changed copy fires at instead.
DROPIN_MINUTE = ":05/15:00"
DROPIN_CHANGED = ":10/15:00"
# The public keyring that verifies InRelease, at the top of the published repository. The test
# image's source names it as /srv/repo/<this>, where the container mounts the repository.
TEST_KEYRING = "homelab-test-keyring.gpg"
# How long a published Release stays valid by default: the real signer's ValidTime.
VALID_FOR = timedelta(days=7)


def box_address(render: Path) -> str:
    """The host of the URI in the homelab.sources a render of homelab-updater ships."""
    sources = (
        render / "packages" / UPDATER / "root/etc/apt/sources.list.d/homelab.sources"
    )
    for line in sources.read_text().splitlines():
        if line.startswith("URIs:"):
            netloc = line.split("//", 1)[1].split("/", 1)[0]
            return netloc.rsplit(":", 1)[0]
    raise ValueError(f"{sources} names no URIs")


def render_fixture(
    variant: str,
    out: Path,
    payload_bytes: int = PAYLOAD_BYTES,
    sops_dir: Path = STAND_IN,
) -> Path:
    """Render fixture `variant` as homelab-daniel-pi under `out`; return its directory.

    `sops_dir` holds the ciphertext a variant that carries `secrets-unit` ships.

    The scratch tree links the repo's real `hosts/lib`, `hosts/keys`, components and shared
    packages, and
    holds the fixture's units and files as `hosts/daniel-pi/`, with the variant's spec as its
    `package.yml`. The payload variant gets `payload_bytes` of random data, which compresses
    to no less.
    """
    with tempfile.TemporaryDirectory() as scratch:
        hosts = Path(scratch) / "hosts"
        hosts.mkdir()
        for name in ("lib", "keys", "components", "packages"):
            (hosts / name).symlink_to(REPO / "hosts" / name)
        package_dir = hosts / HOST
        shutil.copytree(FIXTURE, package_dir, ignore=shutil.ignore_patterns("*.yml"))
        shutil.copy(FIXTURE / f"{variant}.yml", package_dir / "package.yml")
        if variant == "payload":
            (package_dir / "files" / Path(PAYLOAD).name).write_bytes(
                os.urandom(payload_bytes)
            )
        package = host_mode.render_host(HOST, hosts_root=hosts, sops_dir=sops_dir)
        host_mode.write_packages([package], out)
    return out / package.directory


def updater_variant(head: Path, out: Path) -> Path:
    """Copy the head render of homelab-updater under `out`, its unit changed; return it.

    The copy keeps the head render's archive keyring, which every version ships since H3.4c.
    """
    directory = out / "packages" / UPDATER
    shutil.copytree(head / "packages" / UPDATER, directory)
    unit = directory / f"root{UNIT_DIR}/{UPDATER}.service"
    unit.write_text(unit.read_text() + UNIT_CHANGE + "\n")
    return directory


def pi_dropin_variant(head: Path, out: Path) -> Path:
    """Copy the head render of the real homelab-daniel-pi under `out`, its drop-in changed.

    The drop-in on homelab-updater.timer fires at another minute, so its hash changes. The
    manifest records no file hashes, so the copy needs no re-render.

    Raises:
        ValueError: the head render's drop-in does not fire at DROPIN_MINUTE.
    """
    directory = out / "hosts" / HOST
    shutil.copytree(head / "hosts" / HOST, directory, symlinks=True)
    dropin = directory / f"root{DROPIN}"
    text = dropin.read_text()
    if DROPIN_MINUTE not in text:
        raise ValueError(f"{dropin} does not fire at {DROPIN_MINUTE}")
    dropin.write_text(text.replace(DROPIN_MINUTE, DROPIN_CHANGED))
    return directory


def disarmed_inventory(directory: Path) -> Inventory:
    """The repo's inventory with daniel-pi's container-recovery timer disarmed, under `directory`.

    The host_vars directory is copied and the Pi's file gains
    `optimize_pi_recovery_restart_enabled: false`, the edit an operator makes to disarm it, so a
    render through this inventory takes the path a real disarm does.
    """
    host_vars = directory / "host_vars"
    shutil.copytree(REPO_INVENTORY.host_vars, host_vars)
    pi = host_vars / f"{HOST}.yml"
    pi.write_text(pi.read_text() + f"\n{RECOVERY_FLAG}: false\n")
    return dataclasses.replace(REPO_INVENTORY, host_vars=host_vars)


def pi_disarmed_variant(out: Path, sops_dir: Path | None = None) -> Path:
    """Render the real homelab-daniel-pi with its recovery timer disarmed under `out`; return it.

    `sops_dir` holds the ciphertext the package carries; None keeps the repo's own.
    """
    given = {} if sops_dir is None else {"sops_dir": sops_dir}
    with tempfile.TemporaryDirectory() as scratch:
        package = host_mode.render_host(
            HOST, inventory=disarmed_inventory(Path(scratch)), **given
        )
    host_mode.write_packages([package], out)
    return out / package.directory


def build(directory: Path, version: str, out: Path) -> Path:
    """Build the rendered package in `directory` at `version` with nfpm; return the deb."""
    out.mkdir(parents=True)
    subprocess.run(
        ["nfpm", "package", "--config", "nfpm.yaml", "--packager", "deb"]
        + ["--target", f"{out}/"],
        cwd=directory,
        env={**os.environ, "HOMELAB_VERSION": version},
        check=True,
        stdout=subprocess.DEVNULL,
    )
    (deb,) = out.glob("*.deb")
    return deb


def deb_version(deb: Path) -> str:
    return subprocess.run(
        ["dpkg-deb", "--field", str(deb), "Version"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def newer(left: str, right: str) -> bool:
    """Whether Debian version `left` sorts after `right`."""
    result = subprocess.run(
        ["dpkg", "--compare-versions", left, "gt", right], check=False
    )
    return result.returncode == 0


def packages_stanza(deb: Path, filename: str) -> str:
    """The `Packages` stanza apt reads for `deb`, served at `filename`."""
    control = subprocess.run(
        ["dpkg-deb", "--field", str(deb)], capture_output=True, text=True, check=True
    ).stdout.rstrip("\n")
    data = deb.read_bytes()
    return (
        f"{control}\nFilename: {filename}\nSize: {len(data)}\n"
        f"SHA256: {hashlib.sha256(data).hexdigest()}\n"
    )


@functools.cache
def _signing_home() -> Path:
    """A GnuPG home holding one throwaway ed25519 signing key, removed when the process exits."""
    home = Path(tempfile.mkdtemp(prefix="homelab-test-gnupg-"))
    atexit.register(shutil.rmtree, home, ignore_errors=True)
    atexit.register(
        subprocess.run,
        ["gpgconf", "--homedir", str(home), "--kill", "gpg-agent"],
        check=False,
        capture_output=True,
    )
    _gpg(
        home,
        *(
            "--passphrase",
            "",
            "--quick-generate-key",
            "homelab-test <t@example.invalid>",
        ),
        *("ed25519", "sign", "never"),
    )
    return home


def _gpg(home: Path, *argv: str) -> bytes:
    return subprocess.run(
        [
            "gpg",
            "--homedir",
            str(home),
            "--batch",
            "--pinentry-mode",
            "loopback",
            *argv,
        ],
        capture_output=True,
        check=True,
        timeout=60,
    ).stdout


def publish(repo: Path, debs: list[Path], valid_until: datetime | None = None) -> None:
    """Make `repo` a signed apt repository serving exactly `debs` in suite `homelab`.

    `InRelease` is the `Release` clearsigned with the throwaway key, and TEST_KEYRING holds
    that key's public half. The `Release` is valid until `valid_until`, or VALID_FOR after its
    `Date` when that is None; a past `valid_until` publishes an index apt refuses as expired.
    The directory itself is kept, because the container bind-mounts it: only its contents
    are replaced.
    """
    for child in ("pool", "dists"):
        shutil.rmtree(repo / child, ignore_errors=True)
    pool = repo / "pool"
    pool.mkdir(parents=True)
    stanzas = []
    for deb in debs:
        shutil.copy(deb, pool / deb.name)
        stanzas.append(packages_stanza(pool / deb.name, f"pool/{deb.name}"))
    index = "\n".join(stanzas).encode()
    binary = repo / "dists" / "homelab" / "main" / "binary-amd64"
    binary.mkdir(parents=True)
    (binary / "Packages").write_bytes(index)
    now = datetime.now().astimezone()
    until = valid_until or now + VALID_FOR
    release = "\n".join(
        [
            "Origin: homelab",
            "Label: homelab",
            "Suite: homelab",
            "Codename: homelab",
            f"Date: {formatdate(now.timestamp(), usegmt=True)}",
            f"Valid-Until: {formatdate(until.timestamp(), usegmt=True)}",
            "Architectures: amd64",
            "Components: main",
            "SHA256:",
            f" {hashlib.sha256(index).hexdigest()} {len(index)} main/binary-amd64/Packages",
        ]
    )
    suite = repo / "dists" / "homelab"
    (suite / "Release").write_text(release + "\n")
    home = _signing_home()
    _gpg(
        home,
        "--clearsign",
        "--output",
        str(suite / "InRelease"),
        str(suite / "Release"),
    )
    (repo / TEST_KEYRING).write_bytes(_gpg(home, "--export"))
