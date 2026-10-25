"""The pins `hosts/bootstrap-host.sh` embeds equal the files they copy, and Renovate bumps both.

nfpm and uv are blocks of `hosts/packages/homelab-tools/package.yml`, copied byte for byte. The
k3s pin is three keys of the `system-upgrade` role's defaults. A bump of one side alone is the
failure: a host rebuilt from the script would install a tool or a k3s that the packages and the
SUC Plans no longer name.

Run: uv run pytest hosts/lib/tests/test_bootstrap_host_pins.py
"""

import json
import re

from _bootstrap_host import group
from lib.repo_paths import REPO

SCRIPT = (REPO / "hosts" / "bootstrap-host.sh").read_text()
TOOLS = (REPO / "hosts" / "packages" / "homelab-tools" / "package.yml").read_text()
DEFAULTS = (REPO / "ansible/roles/k8s/system-upgrade/defaults/main.yml").read_text()
MANAGERS = json.loads((REPO / "renovate.json").read_text())["customManagers"]


def script_pins(script: str) -> str:
    """The nfpm and uv blocks the script embeds, between its heredoc markers."""
    match = re.search(r"<< 'EOF'\n(.*?)\nEOF\n", script, re.DOTALL)
    assert match, "the script no longer embeds a pins heredoc"
    return match[1]


def package_pins(package: str) -> str:
    """The nfpm and uv blocks of homelab-tools' package.yml, without the comments between them."""
    start = re.search(r"^  nfpm:\n", package, re.MULTILINE)
    assert start, "package.yml carries no nfpm block"
    tail = package[start.start() :].splitlines()
    return "\n".join(line for line in tail if not line.startswith("  #"))


def k3s_pins(defaults: str) -> tuple[str, ...]:
    version = group(r"^k3s_version: (\S+)", defaults, re.MULTILINE)
    url = group(r'^k3s_install_script_url: "(.+?)"', defaults, re.MULTILINE)
    sha = group(r"^k3s_install_script_sha256: (\w+)", defaults, re.MULTILINE)
    # suc_rehearsal.py fills the same template the same way.
    return version, url.replace("{{ k3s_version }}", version), sha


def script_k3s(script: str) -> tuple[str, ...]:
    keys = ("K3S_VERSION", "K3S_INSTALL_URL", "K3S_INSTALL_SHA256")
    return tuple(group(rf"^{key}=(\S+)$", script, re.MULTILINE) for key in keys)


def managers_reading_the_script() -> list[dict]:
    return [
        m
        for m in MANAGERS
        if any(
            "bootstrap-host" in pattern for pattern in m.get("managerFilePatterns", [])
        )
    ]


def test_the_embedded_nfpm_and_uv_blocks_equal_the_packages():
    assert script_pins(SCRIPT) == package_pins(TOOLS)


def test_a_package_bump_the_script_did_not_follow_is_flagged():
    bumped = TOOLS.replace("version: 0.13.4", "version: 0.13.5")

    assert script_pins(SCRIPT) != package_pins(bumped)


def test_the_embedded_k3s_pin_equals_the_system_upgrade_defaults():
    assert script_k3s(SCRIPT) == k3s_pins(DEFAULTS)


def test_a_k3s_bump_the_script_did_not_follow_is_flagged():
    bumped = DEFAULTS.replace("k3s_version: v1.37.1+k3s1", "k3s_version: v1.37.2+k3s1")

    assert script_k3s(SCRIPT) != k3s_pins(bumped)


def test_renovate_finds_every_pin_the_script_holds():
    # The tools manager needs the annotation, version and sha256 lines of each of the four
    # blocks, and the k3s manager needs the annotated K3S_VERSION line.
    found = {}
    for manager in managers_reading_the_script():
        for pattern in manager["matchStrings"]:
            found[
                manager["depNameTemplate"] if "depNameTemplate" in manager else "tools"
            ] = len(re.findall(pattern.replace("(?<", "(?P<"), SCRIPT))
    assert found == {"tools": 4, "k3s-io/k3s": 1}
