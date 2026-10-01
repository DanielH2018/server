#!/usr/bin/env python3
"""Jellyfin's plugin allowlist sweep keeps exactly what the role installs, and nothing else.

`sweep-unlisted-plugins` walks the top-level entries of /config/data/plugins and removes a
`<Name>_<Version>` directory unless `<Name>` is in its KEEP tuple. Three things decide whether
that is safe:

- **KEEP equals the set the installers write.** A name missing from KEEP makes the sweep remove
  a plugin the role installs; a name KEEP carries that no installer writes lets an unmanaged
  plugin survive. The set is derived from the installers' own `PLUGINS / ("<Name>_" + VERSION)`
  lines, so a sixth installer fails here rather than on the PVC.
- **`configurations/` is never swept.** It holds every plugin's settings, and git holds none of
  them.
- **It runs before the installers**, so a KEEP gap costs a re-download, not the plugin.

Run: uv run pytest ansible/tests/services/test_jellyfin_plugin_allowlist.py
"""

import ast
import re
import sys
import textwrap

import pytest

from _helpers import ANSIBLE
from lib.proc_testing import run

DEPLOYMENT = ANSIBLE / "roles" / "k8s" / "jellyfin" / "templates" / "deployment.yaml.j2"

CONTAINER = "- name: sweep-unlisted-plugins"
PLUGINS_LINE = 'PLUGINS = Path("/config/data/plugins")'
INSTALLER_DIR = re.compile(r'PLUGINS / \("([^"]+)_" \+ VERSION\)')


def _script(template: str) -> str:
    """The sweep's Python, cut from its container's `- |` block."""
    body = template.split(CONTAINER, 1)[1]
    block = body.split("- |\n", 1)[1].split("{{ hardened_security_context", 1)[0]
    return textwrap.dedent(block)


def _keep(script: str) -> tuple[str, ...]:
    for node in ast.walk(ast.parse(script)):
        if isinstance(node, ast.Assign) and [
            getattr(t, "id", None) for t in node.targets
        ] == ["KEEP"]:
            return ast.literal_eval(node.value)
    raise AssertionError("the sweep no longer assigns KEEP")


def _installed(template: str) -> set[str]:
    return set(INSTALLER_DIR.findall(template))


def _assert_keep_matches_installers(template: str) -> None:
    keep = set(_keep(_script(template)))
    installed = _installed(template)
    assert keep == installed, (
        f"KEEP and the installers disagree. Installed but not kept: {installed - keep}; the "
        f"sweep would remove those on every start. Kept but not installed: {keep - installed}; "
        f"those are plugins no pin describes, surviving the sweep."
    )


def test_the_installer_census_finds_every_installer():
    """A pattern-found subject must find a named member, or it can pass over an empty set."""
    installed = _installed(DEPLOYMENT.read_text())
    assert "Media Cleaner" in installed and len(installed) >= 5, installed


def test_keep_names_every_installer_and_nothing_else():
    _assert_keep_matches_installers(DEPLOYMENT.read_text())


@pytest.mark.parametrize(
    ("what", "old", "new"),
    [
        ("an installed plugin dropped from KEEP", '"Media Cleaner")', ")"),
        (
            "an uninstalled plugin added to KEEP",
            '"Media Cleaner")',
            '"Media Cleaner", "Trakt")',
        ),
    ],
)
def test_the_guard_rejects_a_keep_that_drifts(what, old, new):
    """Red proof: KEEP out of step with the installers must fail, in both directions."""
    template = DEPLOYMENT.read_text()
    head, tail = template.split(CONTAINER, 1)
    assert old in tail.split("{{ hardened_security_context", 1)[0], (
        f"fixture drift: {what}"
    )
    with pytest.raises(AssertionError):
        _assert_keep_matches_installers(head + CONTAINER + tail.replace(old, new, 1))


def test_the_sweep_runs_before_every_installer():
    template = DEPLOYMENT.read_text()
    assert template.index(CONTAINER) < template.index("- name: install-"), (
        "sweep-unlisted-plugins runs after an installer. A KEEP gap then removes a plugin the "
        "installer just wrote, on every start, instead of costing one re-download."
    )


def _run(script: str, tmp_path) -> str:
    assert PLUGINS_LINE in script, (
        "fixture drift: the sweep no longer names its directory"
    )
    script = script.replace(PLUGINS_LINE, f"PLUGINS = Path({str(tmp_path)!r})")
    done = run([sys.executable, "-c", script], check=True)
    return done.stdout


@pytest.fixture
def plugins(tmp_path):
    """A plugins directory shaped like the live one, plus the entries the sweep exists for."""
    for name in (
        "configurations",
        "Ani-Sync_4.4.0.0",
        "Media Cleaner_3.7.0.101109",
        # Jellyfin's own Update Plugins task writes a newer version between restarts. This
        # sweep keeps both on purpose — it matches the NAME — and the installer's own
        # superseded sweep removes the version the pin does not name
        # (test_jellyfin_plugin_pins_hold_across_a_restart.py).
        "Media Cleaner_99.0.0.0",
        "Trakt_30.0.0.0",
        "SSO Authentication_4.0.0.4",
        "tmpk2j9x",
    ):
        (tmp_path / name).mkdir()
    (tmp_path / "configurations" / "Webhook.xml").write_text("<settings/>")
    (tmp_path / "stray.dll").write_text("")
    return tmp_path


UNLISTED = ("Trakt_30.0.0.0", "SSO Authentication_4.0.0.4", "tmpk2j9x")


def _assert_report(out: str) -> None:
    lines = set(out.splitlines())
    for kept in (
        "Ani-Sync_4.4.0.0",
        "Media Cleaner_3.7.0.101109",
        "Media Cleaner_99.0.0.0",
    ):
        assert "keep " + kept in lines, out
    assert "keep configurations (plugin settings, never swept)" in lines, out
    for unlisted in UNLISTED:
        assert "removing " + unlisted in lines, out
    assert "leave file stray.dll" in lines, out
    assert "allowlist sweep: 3 unlisted plugin(s) removed" in lines, out


def test_the_sweep_removes_only_unlisted_plugin_directories(plugins):
    _assert_report(_run(_script(DEPLOYMENT.read_text()), plugins))
    assert sorted(p.name for p in plugins.iterdir()) == [
        "Ani-Sync_4.4.0.0",
        "Media Cleaner_3.7.0.101109",
        "Media Cleaner_99.0.0.0",
        "configurations",
        "stray.dll",
    ]
    assert (plugins / "configurations" / "Webhook.xml").exists(), (
        "the sweep reached inside configurations/, which holds every plugin's settings."
    )


def test_the_report_check_fails_when_configurations_is_not_skipped(plugins):
    """Red proof: a sweep that treats `configurations` as a plugin must fail the report check."""
    script = _script(DEPLOYMENT.read_text()).replace(
        'SETTINGS = "configurations"', 'SETTINGS = "settings"'
    )
    with pytest.raises(AssertionError):
        _assert_report(_run(script, plugins))


def test_a_fresh_volume_is_not_an_error(tmp_path):
    assert "nothing to sweep" in _run(
        _script(DEPLOYMENT.read_text()), tmp_path / "absent"
    )
