"""The rendered `homelab-updater` package: its named members, and its timeout above reconcile's.

Run: uv run pytest hosts/lib/tests/test_updater_package.py
"""

import re

import pytest
from _reconcile_component import render_reconcile

from lib.repo_paths import REPO
from renderer.renderer_lib import host as host_mode

UPDATER_SCRIPT = REPO / "hosts" / "lib" / "homelab-updater"
SERVICE = "/usr/lib/systemd/system/homelab-updater.service"
TIMER = "/usr/lib/systemd/system/homelab-updater.timer"
SOURCES = "/etc/apt/sources.list.d/homelab.sources"
KEYRING = "/usr/share/keyrings/homelab-archive-keyring.gpg"
KEYRING_FILE = REPO / "hosts" / "keys" / "homelab-archive-keyring.gpg"
RECONCILE_SERVICE = "/usr/lib/systemd/system/homelab-reconcile.service"

_SPAN = re.compile(r"(\d+)\s*(h|min|m|s)?")
_UNITS = {"h": 3600, "min": 60, "m": 60, "s": 1, None: 1}


@pytest.fixture(scope="module")
def package() -> host_mode.Package:
    return host_mode.render_package("homelab-updater")


@pytest.fixture(scope="module")
def reconcile_unit(tmp_path_factory) -> str:
    """The rendered `homelab-reconcile.service`, from the `reconcile` component."""
    rendered = render_reconcile(tmp_path_factory.mktemp("reconcile"))
    return rendered.files[RECONCILE_SERVICE][0].decode()


def test_the_package_renders_its_named_members(package):
    assert sorted(package.files) == sorted(
        [SOURCES, "/usr/lib/homelab/homelab-updater", SERVICE, TIMER, KEYRING]
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
    assert f"Signed-By: {KEYRING}\n" in source


def test_the_package_ships_the_committed_keyring_unchanged(package):
    # The path `Signed-By:` names must hold the operator's export byte for byte (H3.4c).
    assert package.files[KEYRING] == (KEYRING_FILE.read_bytes(), 0o644)


def test_the_package_ships_no_enable_link(package):
    # A host arms its updater by shipping the link in its own package (H3, *Shared names*).
    assert not [dst for dst in package.files if ".wants/" in dst]


def timeout_start_s(unit: str) -> int:
    """The unit's `TimeoutStartSec=` in seconds, from a systemd time span like `1h 20min`."""
    (value,) = re.findall(r"^TimeoutStartSec=(.+)$", unit, re.MULTILINE)
    spans = _SPAN.findall(value)
    if not spans or _SPAN.sub("", value).strip():
        raise ValueError(f"TimeoutStartSec={value} is not a span this test reads")
    return sum(int(number) * _UNITS[suffix or None] for number, suffix in spans)


def lock_timeout_s(script: str | None = None) -> int:
    """Every `DPkg::Lock::Timeout` in the updater, summed: one run can wait on each in turn."""
    text = UPDATER_SCRIPT.read_text() if script is None else script
    waits = [int(value) for value in re.findall(r"DPkg::Lock::Timeout=(\d+)", text)]
    assert waits, "the updater names no DPkg::Lock::Timeout"
    return sum(waits)


def timeout_holds(
    updater_unit: str, reconcile_unit: str, script: str | None = None
) -> bool:
    return timeout_start_s(updater_unit) > timeout_start_s(
        reconcile_unit
    ) + lock_timeout_s(script)


def test_the_updater_timeout_exceeds_reconcile_plus_the_lock_wait_is_clean(
    package, reconcile_unit
):
    assert timeout_holds(package.files[SERVICE][0].decode(), reconcile_unit)
    assert timeout_start_s(reconcile_unit) == 30 * 60


def test_an_updater_timeout_of_20_minutes_is_flagged(reconcile_unit):
    assert not timeout_holds("[Service]\nTimeoutStartSec=20min\n", reconcile_unit)


def test_a_reconcile_timeout_raised_to_45_minutes_is_flagged(package, reconcile_unit):
    # The rendered unit with only its limit changed, so a unit that moved the directive
    # elsewhere, or dropped it, fails the substitution rather than passing on a stand-in.
    raised, count = re.subn(
        r"^TimeoutStartSec=.+$", "TimeoutStartSec=45min", reconcile_unit, flags=re.M
    )
    assert count == 1
    assert not timeout_holds(package.files[SERVICE][0].decode(), raised)


def test_a_step_6_retry_that_waits_on_the_lock_too_is_flagged(package, reconcile_unit):
    # Two 600s waits and reconcile's 30 minutes fill the whole 50-minute limit.
    retrying = UPDATER_SCRIPT.read_text().replace(
        "DPkg::Lock::Timeout=0", "DPkg::Lock::Timeout=600"
    )
    assert not timeout_holds(
        package.files[SERVICE][0].decode(), reconcile_unit, retrying
    )


@pytest.mark.parametrize(
    ("span", "seconds"), [("50min", 3000), ("1h 20min", 4800), ("90", 90), ("30s", 30)]
)
def test_a_timeout_span_reads_as_seconds(span, seconds):
    assert timeout_start_s(f"TimeoutStartSec={span}\n") == seconds
