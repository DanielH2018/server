"""The rendered `reconcile` component, and `homelab-reconcile` held to the 3.12 grammar.

The hosts run reconcile under Ubuntu 24.04's python3, which is 3.12, while this suite runs a
newer interpreter. The parse below covers syntax only; the container suite, which runs
reconcile under 3.12, covers the standard library.

Run: uv run pytest hosts/lib/tests/test_reconcile_component.py
"""

import ast

import pytest
import yaml
from _reconcile_component import render_reconcile

from lib.repo_paths import REPO
from renderer.renderer_lib import host as host_mode

RECONCILE = REPO / "hosts" / "lib" / "homelab-reconcile"
LIBRARY = REPO / "hosts" / "lib" / "reconcile_lib"
SCRIPT = "/usr/lib/homelab/homelab-reconcile"
MODULES = "/usr/lib/homelab/reconcile_lib"
# Modules the package must hold, so a glob that stopped finding them cannot pass the tests below.
LIBRARY_MEMBERS = frozenset(
    {
        "activate.py",
        "common.py",
        "libvirt.py",
        "lines.py",
        "textfile.py",
        "ufw.py",
        "users.py",
    }
)
SERVICE = "/usr/lib/systemd/system/homelab-reconcile.service"
METRICS_SERVICE = "/usr/lib/systemd/system/homelab-reconcile-metrics.service"
MANIFEST = "/usr/lib/homelab/manifest.json"


@pytest.fixture(scope="module")
def package(tmp_path_factory) -> host_mode.Package:
    return render_reconcile(tmp_path_factory.mktemp("reconcile"))


def library_files() -> list:
    return sorted(LIBRARY.glob("*.py"))


def test_the_library_glob_finds_its_members():
    assert LIBRARY_MEMBERS <= {path.name for path in library_files()}


def test_the_component_renders_the_script_its_library_and_its_unit(package):
    modules = [f"{MODULES}/{path.name}" for path in library_files()]
    assert sorted(package.files) == sorted(
        [SCRIPT, *modules, SERVICE, METRICS_SERVICE, MANIFEST]
    )
    assert package.files[SCRIPT] == (RECONCILE.read_bytes(), 0o755)
    for path in library_files():
        assert package.files[f"{MODULES}/{path.name}"] == (path.read_bytes(), 0o644)
    assert package.units() == [
        "homelab-reconcile-metrics.service",
        "homelab-reconcile.service",
    ]
    unit = package.files[SERVICE][0].decode()
    assert "Type=oneshot\n" in unit
    assert f"ExecStart={SCRIPT}\n" in unit


def test_the_script_runs_under_the_hosts_python3():
    assert RECONCILE.read_text().startswith("#!/usr/bin/python3\n")
    assert RECONCILE.stat().st_mode & 0o111


def parses_under_3_12(source: str) -> bool:
    try:
        ast.parse(source, feature_version=(3, 12))
    except SyntaxError:
        return False
    return True


def test_reconcile_and_its_library_parse_with_the_3_12_grammar():
    for path in [RECONCILE, *library_files()]:
        assert parses_under_3_12(path.read_text()), path


@pytest.mark.parametrize(
    "newer",
    ['label = t"{name}"\n', "try:\n    pass\nexcept OSError, ValueError:\n    pass\n"],
    ids=["t-string", "unparenthesised-except"],
)
def test_syntax_newer_than_3_12_is_flagged(newer):
    assert not parses_under_3_12(RECONCILE.read_text() + newer)


def test_the_metrics_unit_rewrites_the_textfile_once_per_boot(package):
    """The boot path runs it through its enable link, and RemainAfterExit keeps reconcile's
    step 2 from starting it again on every run (#6188)."""
    unit = package.files[METRICS_SERVICE][0].decode()
    assert f"ExecStart={SCRIPT} --metrics\n" in unit
    assert "Type=oneshot\n" in unit
    assert "RemainAfterExit=yes\n" in unit
    assert package.links == {
        "/usr/lib/systemd/system/multi-user.target.wants/homelab-reconcile-metrics.service": (
            "../homelab-reconcile-metrics.service"
        )
    }


def test_a_leveled_reconcile_line_carries_its_priority_only_on_the_journal(
    tmp_path, monkeypatch, capsys
):
    # The distro tier's copy of host_lib's prefix rule (#6202): `journalctl -p err` finds a
    # reconcile failure, and a terminal never sees `<3>`.
    from reconcile_lib import common

    journal = tmp_path / "journal"
    journal.touch()
    st = journal.stat()
    monkeypatch.setenv("JOURNAL_STREAM", f"{st.st_dev}:{st.st_ino}")
    with open(journal, "w") as out:
        common.log(
            "stop x failed:\nboom", common.ERR, tag="homelab-reconcile", stream=out
        )
        common.log("stopped x", tag="homelab-reconcile", stream=out)
    assert journal.read_text() == (
        "<3>homelab-reconcile: stop x failed:\n<3>boom\nhomelab-reconcile: stopped x\n"
    )
    common.log("stop x failed", common.ERR)
    assert capsys.readouterr().err == "homelab-reconcile: stop x failed\n"


def test_every_host_package_with_the_secrets_unit_ships_reconcile():
    """homelab-secrets imports reconcile_lib's `log`, which only the reconcile component ships."""
    packages = {
        path.parent.name: yaml.safe_load(path.read_text()) or {}
        for path in (REPO / "hosts").glob("*/package.yml")
    }
    with_secrets = {
        host: package["components"]
        for host, package in packages.items()
        if "secrets-unit" in package.get("components", [])
    }
    assert {"daniel-box", "daniel-pi", "daniel-server"} <= set(with_secrets)
    missing = sorted(
        host for host, parts in with_secrets.items() if "reconcile" not in parts
    )
    assert not missing, f"{missing} ship homelab-secrets without reconcile_lib"
