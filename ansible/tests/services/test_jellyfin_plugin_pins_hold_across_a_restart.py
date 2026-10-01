#!/usr/bin/env python3
"""Jellyfin's plugin installers hold their pin on a start where the plugin is already installed.

Jellyfin's own `Update Plugins` scheduled task downloads a newer build of an installed plugin
into its own `<Name>_<Version>` directory, seconds after startup, and the server then loads the
newest one it finds. An installer that exited on `already installed` BEFORE its
superseded-version sweep would leave that unpinned directory in place across every restart,
running a build no checksum verified.

Two behaviours prevent that, and both live on the path where the pinned version is already on the
PVC:

- the superseded sweep runs, removing a sibling directory the pin does not name;
- `autoUpdate: false` is written into the installed plugin's `meta.json`, which is what stops
  `Update Plugins` fetching another one (`InstallationManager.GetAvailablePluginUpdates` skips a
  plugin whose manifest carries it, Jellyfin v10.11.11).

The scripts run for real against a temporary plugins directory. Nothing here reaches the
network: the pinned directory is seeded first, so every installer takes its already-installed
branch.

Run: uv run pytest ansible/tests/services/test_jellyfin_plugin_pins_hold_across_a_restart.py
"""

import ast
import json
import re
import sys
import textwrap

import pytest

from _helpers import ANSIBLE, load_defaults
from lib.proc_testing import run

JELLYFIN = ANSIBLE / "roles" / "k8s" / "jellyfin"
DEPLOYMENT = JELLYFIN / "templates" / "deployment.yaml.j2"

# Every installer the deployment declares, by container name. Named rather than globbed: a
# census that finds its subjects by pattern passes over an empty set once they are renamed.
INSTALLERS = (
    "install-ani-sync",
    "install-intro-skipper",
    "install-webhook",
    "install-merge-versions",
    "install-media-cleaner",
)

PLUGINS_LINE = 'PLUGINS = Path("/config/data/plugins")'
PLUGIN_DIR_LINE = re.compile(r'PLUGINS / \("([^"]+)_" \+ VERSION\)')
JINJA_VAR = re.compile(r"\{\{\s*([a-z0-9_]+)\s*\}\}")


def _script(container: str) -> str:
    """The installer's Python, cut from its container's `- |` block, with the vars rendered."""
    template = DEPLOYMENT.read_text()
    body = template.split("- name: " + container, 1)[1]
    block = body.split("- |\n", 1)[1].split("{{ hardened_security_context", 1)[0]
    defaults = load_defaults(JELLYFIN)
    return JINJA_VAR.sub(lambda m: str(defaults[m.group(1)]), textwrap.dedent(block))


def _literal(script: str, name: str) -> str | None:
    for node in ast.walk(ast.parse(script)):
        if isinstance(node, ast.Assign) and [
            getattr(t, "id", None) for t in node.targets
        ] == [name]:
            return ast.literal_eval(node.value)
    return None


def _seed(script: str, plugins) -> str:
    """The pinned plugin, complete, plus the newer directory `Update Plugins` leaves behind."""
    name = PLUGIN_DIR_LINE.search(script).group(1)
    version = _literal(script, "VERSION")
    pinned = plugins / f"{name}_{version}"
    pinned.mkdir(parents=True)
    for dll in (_literal(script, "DLL"), _literal(script, "DEP_DLL")):
        if dll:
            (pinned / dll).write_text("")
    (pinned / "meta.json").write_text(json.dumps({"name": name, "version": version}))

    newer = plugins / f"{name}_99.0.0.0"
    newer.mkdir()
    (newer / _literal(script, "DLL")).write_text("")
    return name


def _run(script: str, plugins) -> str:
    assert PLUGINS_LINE in script, (
        "fixture drift: the installer no longer names its directory"
    )
    script = script.replace(PLUGINS_LINE, f"PLUGINS = Path({str(plugins)!r})")
    done = run([sys.executable, "-c", script], check=True)
    return done.stdout


def _assert_pin_holds(script: str, plugins) -> None:
    name = _seed(script, plugins)
    version = _literal(script, "VERSION")
    out = _run(script, plugins)

    assert "already installed" in out, (
        f"the installer downloaded instead of taking its already-installed branch: {out}"
    )
    assert sorted(p.name for p in plugins.iterdir()) == [f"{name}_{version}"], (
        f"a directory the pin does not name survived the sweep. Jellyfin loads the newest "
        f"version it finds, so an unpinned build keeps running behind a green rollout.\n{out}"
    )


def _assert_auto_update_off(script: str, plugins) -> None:
    name = _seed(script, plugins)
    version = _literal(script, "VERSION")
    _run(script, plugins)

    meta = json.loads((plugins / f"{name}_{version}" / "meta.json").read_text())
    assert meta.get("autoUpdate") is False, (
        f"{name}'s meta.json does not carry autoUpdate: false, so Jellyfin's `Update Plugins` "
        f"task downloads an unpinned build again on the next run: {meta}"
    )


def test_the_census_finds_every_installer():
    """A named census, so a renamed container fails here rather than testing nothing."""
    template = DEPLOYMENT.read_text()
    missing = [c for c in INSTALLERS if "- name: " + c not in template]
    assert not missing, f"the deployment no longer declares: {missing}"


@pytest.mark.parametrize("container", INSTALLERS)
def test_an_installer_sweeps_a_newer_sibling_it_did_not_install(container, tmp_path):
    _assert_pin_holds(_script(container), tmp_path)


@pytest.mark.parametrize("container", INSTALLERS)
def test_an_installer_turns_auto_update_off_on_every_start(container, tmp_path):
    _assert_auto_update_off(_script(container), tmp_path)


def test_the_sweep_guard_rejects_an_installer_that_exits_early(tmp_path):
    """Red proof: an early exit before the sweep must fail the check."""
    script = _script("install-media-cleaner")
    early = '    print("media-cleaner " + VERSION + " already installed")'
    assert early in script, "fixture drift: the already-installed branch moved"
    with pytest.raises(AssertionError):
        _assert_pin_holds(
            script.replace(early, early + "\n    raise SystemExit(0)", 1), tmp_path
        )


def test_the_auto_update_guard_rejects_an_installer_that_leaves_the_flag_alone(
    tmp_path,
):
    """Red proof: an installer that writes no flag must fail the check."""
    script = _script("install-media-cleaner")
    victim = '        meta["autoUpdate"] = False'
    assert victim in script, "fixture drift: the autoUpdate pin moved"
    with pytest.raises(AssertionError):
        _assert_auto_update_off(script.replace(victim, "        pass", 1), tmp_path)
