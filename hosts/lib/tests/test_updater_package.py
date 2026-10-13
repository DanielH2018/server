"""The rendered `homelab-updater` package: its named members, and its timeout above reconcile's.

Run: uv run pytest hosts/lib/tests/test_updater_package.py
"""

import re

import pytest

from lib.repo_paths import REPO
from renderer.renderer_lib import host as host_mode

UPDATER_SCRIPT = REPO / "hosts" / "lib" / "homelab-updater"
SERVICE = "/usr/lib/systemd/system/homelab-updater.service"
TIMER = "/usr/lib/systemd/system/homelab-updater.timer"
SOURCES = "/etc/apt/sources.list.d/homelab.sources"

# TODO: H3.2b - read this from the rendered homelab-reconcile.service once the reconcile
# component ships it; H3's *The reconcile unit* fixes it at 30 minutes.
RECONCILE_TIMEOUT_S = 30 * 60

_SPAN = re.compile(r"(\d+)\s*(h|min|m|s)?")
_UNITS = {"h": 3600, "min": 60, "m": 60, "s": 1, None: 1}


@pytest.fixture(scope="module")
def package() -> host_mode.Package:
    return host_mode.render_package("homelab-updater")


def test_the_package_renders_its_named_members(package):
    assert sorted(package.files) == sorted(
        [SOURCES, "/usr/lib/homelab/homelab-updater", SERVICE, TIMER]
    )
    assert package.files["/usr/lib/homelab/homelab-updater"] == (
        UPDATER_SCRIPT.read_bytes(),
        0o755,
    )
    assert sorted(package.scripts) == ["postinst", "prerm"]
    assert package.depends == ["python3 (>= 3.12)"]


def test_the_source_names_daniel_box_and_the_keyring_path(package):
    box = host_mode.host_context(None, {})[0]["hostvars"]["daniel-box"]["server_ip"]
    source = package.files[SOURCES][0].decode()
    assert f"URIs: http://{box}:8791\n" in source
    assert "Signed-By: /usr/share/keyrings/homelab-archive-keyring.gpg\n" in source


def test_the_package_ships_no_enable_link_and_no_keyring(package):
    # A host arms its updater by shipping the link in its own package (H3, *Shared names*).
    # The keyring joins in H3.4c, with the key.
    assert not [dst for dst in package.files if ".wants/" in dst]
    assert not [dst for dst in package.files if dst.startswith("/usr/share/keyrings/")]


def timeout_start_s(unit: str) -> int:
    """The unit's `TimeoutStartSec=` in seconds, from a systemd time span like `1h 20min`."""
    (value,) = re.findall(r"^TimeoutStartSec=(.+)$", unit, re.MULTILINE)
    spans = _SPAN.findall(value)
    if not spans or _SPAN.sub("", value).strip():
        raise ValueError(f"TimeoutStartSec={value} is not a span this test reads")
    return sum(int(number) * _UNITS[suffix or None] for number, suffix in spans)


def lock_timeout_s() -> int:
    """Step 5's `DPkg::Lock::Timeout`, read from the updater script."""
    (value,) = set(re.findall(r"DPkg::Lock::Timeout=(\d+)", UPDATER_SCRIPT.read_text()))
    return int(value)


def timeout_holds(updater_unit: str) -> bool:
    return timeout_start_s(updater_unit) > RECONCILE_TIMEOUT_S + lock_timeout_s()


def test_the_updater_timeout_exceeds_reconcile_plus_the_lock_wait_is_clean(package):
    assert timeout_holds(package.files[SERVICE][0].decode())


def test_an_updater_timeout_of_20_minutes_is_flagged():
    assert not timeout_holds("[Service]\nTimeoutStartSec=20min\n")


@pytest.mark.parametrize(
    ("span", "seconds"), [("50min", 3000), ("1h 20min", 4800), ("90", 90), ("30s", 30)]
)
def test_a_timeout_span_reads_as_seconds(span, seconds):
    assert timeout_start_s(f"TimeoutStartSec={span}\n") == seconds
