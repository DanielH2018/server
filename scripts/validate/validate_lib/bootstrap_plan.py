"""The bootstrap cases' plan: each case's host, kit and seed, and the verdict it expects.

`bootstrap_case.run` runs what `plan` lists. The plan reads the constants and the pins out of
`hosts/bootstrap-host.sh`, so it sits apart from the containers that run it.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import re
import shutil
from pathlib import Path
from typing import NamedTuple

from lib.repo_paths import REPO
from validate.validate_lib.fixture_host_case import FIXTURE_HOST
from validate.validate_lib.host_container import CaseFailed
from validate.validate_lib.scratch_clones import ANCHOR_ACCEPTS, HOMELAB_ONLY

SCRIPT = REPO / "hosts" / "bootstrap-host.sh"
UNCHECKED = "is unchecked; the operator who chose it is its trust root"
SEED_DIR = "bootstrap-seed"
INSTALL_REFUSED = "apt-get install of the local build failed"
# install.sh's verify_binary prints this after download_binary.
K3S_VERIFIED = "Verifying binary download"


def script_value(script: str, name: str) -> str:
    """The value of a top-level `NAME=value` line of the script, unquoted."""
    match = re.search(rf"^{name}=(.*)$", script, re.MULTILINE)
    if not match:
        raise CaseFailed(f"bootstrap-host.sh sets no {name}")
    return match[1].strip('"')


def kit_pins(script: str) -> dict[str, tuple[str, str]]:
    """Each amd64 kit file the script checks against a pin, by file name: (url, sha256)."""
    pins = {
        "install.sh": (
            script_value(script, "K3S_INSTALL_URL"),
            script_value(script, "K3S_INSTALL_SHA256"),
        )
    }
    for tool in ("nfpm", "uv"):
        block = re.search(rf"^  {tool}:\n    amd64:\n((?:      .*\n)+)", script, re.M)
        if not block:
            raise CaseFailed(f"bootstrap-host.sh's PINS holds no amd64 {tool}")
        fields = dict(re.findall(r"^      (\w+): (\S+)$", block[1], re.MULTILINE))
        version = fields["version"]
        url = fields["url"].replace("{version}", version)
        url = url.replace("{bare}", version.removeprefix("v"))
        pins[url.rsplit("/", 1)[1]] = (url, fields["sha256"])
    return pins


def red_kit(green: Path, out: Path, name: str, data: bytes | None) -> Path:
    """The green kit with `name` replaced by `data`, or left out when `data` is None.

    Every other entry is a symlink into the green kit, which the containers mount at its own
    path, so a red kit costs no copy of uv's cache.
    """
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    for entry in green.iterdir():
        if entry.name != name:
            (out / entry.name).symlink_to(entry)
    if data is not None:
        (out / name).write_bytes(data)
    return out


class Planned(NamedTuple):
    """One bootstrap case: the host and kit it runs with, and the verdict it expects.

    Attributes:
        name: The case's name, as the job log prints it.
        label: The container's label.
        host: The host the script runs for.
        kit: The offline kit, or None for an online run.
        marker: A line a green run must print; None for a red one.
        refusal: The refusal a red run must print; None for a green one.
        clone: Which scratch clone the run builds: "green", "red-depends" for `red_clone`'s, or
            "fixture-host" for `fixture_clone`'s.
        apt_says: What apt must print when a red run's refusal is apt's; None when the
            refusal comes before any install.
        seed: The directory `--tools-seed` reads, or None for an offline run, which has the kit.
        absent: A line a green run must not print, or None for the default: the anchor's
            accept line offline, and nothing online.
        gates: False for the case only `--k3s-download` runs, outside the PR's verdict.
    """

    name: str
    label: str
    host: str
    kit: Path | None
    marker: str | None
    refusal: str | None
    clone: str = "green"
    apt_says: str | None = None
    seed: Path | None = None
    absent: str | None = None
    gates: bool = True


def plan(work: Path, green: Path) -> list[Planned]:
    """Every bootstrap case, in order, writing each red kit beside the green one."""
    script = SCRIPT.read_text()
    # DECIDED: every online case seeds k3s, so no case exercises install.sh's own binary
    # download or its check of that binary. One unseeded case fails the run whenever a GitHub
    # release outage outlasts `retry`'s window (#7625, #7729), and install.sh still writes the
    # unit that unit_problems reads. The `k3s-download` case below covers that download, in
    # the `continue-on-error` step of ci.yml's `host_packages` job (#7754).
    cases = [
        Planned(
            f"bootstrap {host} with --local-build",
            host,
            host,
            None,
            ANCHOR_ACCEPTS,
            None,
            seed=work / SEED_DIR,
        )
        for host in script_value(script, "HOSTS").split()
    ]
    # The kit's tools-cache never holds k3s, so install.sh downloads the release binary here.
    cases.append(
        Planned(
            "bootstrap daniel-box with --local-build, downloading k3s from its release",
            "k3s-download",
            "daniel-box",
            None,
            K3S_VERIFIED,
            None,
            seed=green / "tools-cache",
            absent="Skipping k3s download and verify",  # INSTALL_K3S_SKIP_DOWNLOAD's line
            gates=False,
        )
    )
    cases.append(
        Planned(
            "bootstrap daniel-box offline from the kit",
            "offline",
            "daniel-box",
            green,
            UNCHECKED,
            None,
        )
    )
    nfpm = next(name for name in kit_pins(script) if name.startswith("nfpm"))
    reds = (
        ("k3s", None, "the offline kit has no k3s"),
        (nfpm, b"not the pinned nfpm", f"{nfpm} does not match its pinned sha256"),
    )
    for name, data, reason in reds:
        kit = red_kit(green, work / "bootstrap-kit" / f"red-{name}", name, data)
        label = f"red-{name.split('_')[0]}"
        # The name says which fixture failed: a dropped file and a changed one are different reds.
        defect = "is missing" if data is None else "does not match its pin"
        cases.append(
            Planned(
                f"refuse a kit whose {name} {defect}",
                label,
                "daniel-box",
                kit,
                None,
                reason,
            )
        )
    # Offline, so the red needs no network and differs from the green offline case in its
    # clone alone.
    cases.append(
        Planned(
            f"refuse a local build whose homelab-daniel-box Depends on {HOMELAB_ONLY}",
            "red-depends",
            "daniel-box",
            green,
            None,
            INSTALL_REFUSED,
            "red-depends",
            f"Depends: {HOMELAB_ONLY}",
        )
    )
    # Offline, like the other cases that differ from the green build in their clone alone.
    cases.append(
        Planned(
            f"bootstrap {FIXTURE_HOST}, made from the Adding a host walkthrough",
            "fixture-host",
            FIXTURE_HOST,
            green,
            UNCHECKED,
            None,
            "fixture-host",
        )
    )
    return cases


def selected(planned: list[Planned], k3s_download: bool) -> list[Planned]:
    """The cases one run takes: the gating ones, or under `--k3s-download` the rest."""
    return [item for item in planned if item.gates != k3s_download]
