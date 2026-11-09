"""The maintainer-script lint: every rendered script holds only the line shapes C5 allows.

H3's *Package rules*: `postinst` reloads systemd and starts reconcile without blocking,
`prerm` stops the package's own `homelab-*` units only on `remove`, and `preinst` refuses a
hostname mismatch. Nothing else runs as root at install time. The whitelist admits no
`dpkg-divert` line; H7's *In-place edits* adds those shapes with the first diversion.

The whitelist and `lint_script` live in `scripts/validate/host_packages.py`, which CI's
`host_packages` job runs over each build. The tests here run it over every package the tree
renders.

Run: uv run pytest hosts/lib/tests/test_maintainer_scripts.py
"""

import functools

import pytest

from lib import yaml_fast
from lib.repo_paths import REPO
from renderer.renderer_lib import host as host_mode
from validate.host_packages import lint_script as lint


def _declared() -> dict[str, list[str]]:
    """Each package's `maintainer_scripts`, read from its spec rather than rendered.

    A render at import time costs every xdist worker ~9s of collection, which no
    `--durations` phase or shard weight sees (#7710).
    """
    specs = {
        spec.parent.name: spec
        for spec in (REPO / "hosts" / "packages").glob("*/package.yml")
    }
    for spec in (REPO / "hosts").glob("*/package.yml"):
        specs[f"homelab-{spec.parent.name}"] = spec
    return {
        name: list(
            yaml_fast.safe_load(spec.read_text()).get("maintainer_scripts") or []
        )
        for name, spec in sorted(specs.items())
    }


SHIPPED = _declared()


@functools.cache
def _rendered(package: str) -> host_mode.Package:
    if (REPO / "hosts" / "packages" / package / "package.yml").is_file():
        return host_mode.render_package(package)
    return host_mode.render_host(package.removeprefix("homelab-"))


def test_the_shipped_scripts_include_the_updaters():
    assert {"postinst", "prerm"} <= set(SHIPPED["homelab-updater"])
    assert {"postinst", "prerm"} <= set(_rendered("homelab-updater").scripts)


def test_each_package_renders_exactly_the_scripts_its_spec_declares():
    """The parametrize below reads the specs, so a render that adds a script would go unlinted."""
    rendered = {name: sorted(_rendered(name).scripts) for name in SHIPPED}
    assert rendered == {name: sorted(scripts) for name, scripts in SHIPPED.items()}


@pytest.mark.parametrize(
    ("package", "script"),
    [(name, script) for name, scripts in SHIPPED.items() for script in scripts],
)
def test_every_shipped_maintainer_script_is_clean(package, script):
    text = _rendered(package).scripts[script].decode()
    assert lint(script, text) == []


def test_the_updaters_prerm_stops_its_own_units_on_remove_only():
    prerm = _rendered("homelab-updater").scripts["prerm"].decode()
    assert prerm.splitlines()[-5:] == [
        'if [ "$1" = remove ]; then',
        "    systemctl stop homelab-updater.service || true",
        "    systemctl stop homelab-updater.timer || true",
        "fi",
        "exit 0",
    ]


def test_a_rendered_preinst_is_clean(tmp_path):
    # A fixture host renders the real template, so the test reads preinst alone.
    hosts = tmp_path / "hosts"
    templates = hosts / "lib" / "maintainer-scripts"
    templates.mkdir(parents=True)
    template = REPO / "hosts" / "lib" / "maintainer-scripts" / "preinst.j2"
    (templates / "preinst.j2").write_text(template.read_text())
    (hosts / "daniel-pi").mkdir()
    (hosts / "daniel-pi" / "package.yml").write_text("maintainer_scripts: [preinst]\n")

    preinst = host_mode.render_host("daniel-pi", hosts).scripts["preinst"].decode()

    assert 'if [ "$(hostname)" != "daniel-pi" ]; then' in preinst
    assert lint("preinst", preinst) == []


@pytest.mark.parametrize(
    ("name", "line"),
    [
        ("postinst", "rm -rf /etc/foo"),
        ("postinst", "dpkg-divert --add --rename /etc/nut/upsmon.conf"),
        ("prerm", "    systemctl stop ssh.service || true"),
        ("prerm", "systemctl stop homelab-updater.timer"),
        ("preinst", "hostname daniel-pi"),
    ],
)
def test_a_line_outside_the_whitelist_is_flagged(name, line):
    text = f"#!/bin/sh\n{line}\nexit 0\n"
    assert lint(name, text) == [f"{name}:2: {line!r}"]


def test_a_script_name_outside_the_whitelist_is_flagged():
    assert lint("postrm", "#!/bin/sh\nexit 0\n") != []
