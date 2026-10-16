#!/usr/bin/env python3
"""The packages and the repository the host_packages container cases serve.

`scripts/validate/host_package_cases.py` runs the cases. This module builds what they install:
the fixture homelab-daniel-pi in three versions, rendered by the real renderer from
`hosts/lib/tests/container/fixture/`; a homelab-updater whose unit changes; a copy of the real
homelab-daniel-pi whose drop-in on the updater's timer changes; and the unsigned apt
repository, in suite `homelab`, that serves a chosen set of them.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import hashlib
import os
import shutil
import subprocess
import tempfile
from email.utils import formatdate
from pathlib import Path

from lib.repo_paths import REPO
from renderer.renderer_lib import host as host_mode

FIXTURE = REPO / "hosts" / "lib" / "tests" / "container" / "fixture"
VARIANTS = ("base", "head", "payload")
HOST = "daniel-pi"
UPDATER = "homelab-updater"
HOST_PKG = f"homelab-{HOST}"
PAYLOAD = "/usr/lib/homelab/fixture-payload"
PAYLOAD_BYTES = 64 * 1024 * 1024
KEYRING = "/usr/share/keyrings/homelab-archive-keyring.gpg"
UNIT_DIR = "/usr/lib/systemd/system"
UNIT_CHANGE = "Environment=HOMELAB_FIXTURE=unit-changed"
DROPIN = f"{UNIT_DIR}/{UPDATER}.timer.d/50-homelab.conf"
# The real Pi drop-in's minute, and the one its changed copy fires at instead.
DROPIN_MINUTE = ":05/15:00"
DROPIN_CHANGED = ":10/15:00"


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


def render_fixture(variant: str, out: Path, payload_bytes: int = PAYLOAD_BYTES) -> Path:
    """Render fixture `variant` as homelab-daniel-pi under `out`; return its directory.

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
        package = host_mode.render_host(HOST, hosts_root=hosts)
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
    shutil.copytree(head / "hosts" / HOST, directory)
    dropin = directory / f"root{DROPIN}"
    text = dropin.read_text()
    if DROPIN_MINUTE not in text:
        raise ValueError(f"{dropin} does not fire at {DROPIN_MINUTE}")
    dropin.write_text(text.replace(DROPIN_MINUTE, DROPIN_CHANGED))
    return directory


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


def publish(repo: Path, debs: list[Path]) -> None:
    """Make `repo` an unsigned apt repository serving exactly `debs` in suite `homelab`.

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
    release = "\n".join(
        [
            "Origin: homelab",
            "Label: homelab",
            "Suite: homelab",
            "Codename: homelab",
            f"Date: {formatdate(usegmt=True)}",
            "Architectures: amd64",
            "Components: main",
            "SHA256:",
            f" {hashlib.sha256(index).hexdigest()} {len(index)} main/binary-amd64/Packages",
        ]
    )
    (repo / "dists" / "homelab" / "Release").write_text(release + "\n")
