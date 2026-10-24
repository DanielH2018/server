"""The rendered `reconcile` component, and `homelab-reconcile` held to the 3.12 grammar.

The hosts run reconcile under Ubuntu 24.04's python3, which is 3.12, while this suite runs a
newer interpreter. The parse below covers syntax only; the container suite, which runs
reconcile under 3.12, covers the standard library.

Run: uv run pytest hosts/lib/tests/test_reconcile_component.py
"""

import ast

import pytest
from _reconcile_component import render_reconcile

from lib.repo_paths import REPO
from renderer.renderer_lib import host as host_mode

RECONCILE = REPO / "hosts" / "lib" / "homelab-reconcile"
LIBRARY = REPO / "hosts" / "lib" / "reconcile_lib"
SCRIPT = "/usr/lib/homelab/homelab-reconcile"
MODULES = "/usr/lib/homelab/reconcile_lib"
# Modules the package must hold, so a glob that stopped finding them cannot pass the tests below.
LIBRARY_MEMBERS = frozenset({"activate.py", "common.py", "textfile.py", "ufw.py"})
SERVICE = "/usr/lib/systemd/system/homelab-reconcile.service"


@pytest.fixture(scope="module")
def package(tmp_path_factory) -> host_mode.Package:
    return render_reconcile(tmp_path_factory.mktemp("reconcile"))


def library_files() -> list:
    return sorted(LIBRARY.glob("*.py"))


def test_the_library_glob_finds_its_members():
    assert LIBRARY_MEMBERS <= {path.name for path in library_files()}


def test_the_component_renders_the_script_its_library_and_its_unit(package):
    modules = [f"{MODULES}/{path.name}" for path in library_files()]
    assert sorted(package.files) == sorted([SCRIPT, *modules, SERVICE])
    assert package.files[SCRIPT] == (RECONCILE.read_bytes(), 0o755)
    for path in library_files():
        assert package.files[f"{MODULES}/{path.name}"] == (path.read_bytes(), 0o644)
    assert package.units() == ["homelab-reconcile.service"]
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
