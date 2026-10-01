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

Each script comes out of the RENDERED Deployment (`_jellyfin_plugins`), which is also what
supplies the pinned version and the DLL names the seed needs. Cutting them out of
`deployment.yaml.j2` meant a hand-rolled `{{ var }}` substitution standing in for Jinja — one that
resolved a role default and could not see an inventory override at all.

Run: uv run pytest ansible/tests/services/test_jellyfin_plugin_pins_hold_across_a_restart.py
"""

import json
import sys

import pytest

from _jellyfin_plugins import (
    INSTALLERS,
    PLUGIN_ROOT,
    constants,
    init_containers,
    script,
)
from lib.proc_testing import run


def _plugin_name(installer: str) -> str:
    """The plugin NAME an installer writes, from its rendered `PLUGIN_DIR`."""
    plugin_dir = constants(installer)["PLUGIN_DIR"]
    return plugin_dir.rsplit("/", 1)[-1].rsplit("_", 1)[0]


def _seed(installer: str, plugins) -> str:
    """The pinned plugin, complete, plus the newer directory `Update Plugins` leaves behind."""
    consts = constants(installer)
    name = _plugin_name(installer)
    version = consts["VERSION"]
    pinned = plugins / f"{name}_{version}"
    pinned.mkdir(parents=True)
    for dll in (consts.get("DLL"), consts.get("DEP_DLL")):
        if dll:
            (pinned / dll).write_text("")
    (pinned / "meta.json").write_text(json.dumps({"name": name, "version": version}))

    newer = plugins / f"{name}_99.0.0.0"
    newer.mkdir()
    (newer / consts["DLL"]).write_text("")
    return name


def _run(installer: str, plugins) -> str:
    """The installer, run for real against `plugins` instead of the PVC."""
    assert PLUGIN_ROOT in installer, (
        "fixture drift: the installer no longer names its directory"
    )
    done = run(
        [sys.executable, "-c", installer.replace(PLUGIN_ROOT, str(plugins))], check=True
    )
    return done.stdout


def _assert_pin_holds(installer: str, plugins) -> None:
    name = _seed(installer, plugins)
    version = constants(installer)["VERSION"]
    out = _run(installer, plugins)

    assert "already installed" in out, (
        f"the installer downloaded instead of taking its already-installed branch: {out}"
    )
    assert sorted(p.name for p in plugins.iterdir()) == [f"{name}_{version}"], (
        f"a directory the pin does not name survived the sweep. Jellyfin loads the newest "
        f"version it finds, so an unpinned build keeps running behind a green rollout.\n{out}"
    )


def _assert_auto_update_off(installer: str, plugins) -> None:
    name = _seed(installer, plugins)
    version = constants(installer)["VERSION"]
    _run(installer, plugins)

    meta = json.loads((plugins / f"{name}_{version}" / "meta.json").read_text())
    assert meta.get("autoUpdate") is False, (
        f"{name}'s meta.json does not carry autoUpdate: false, so Jellyfin's `Update Plugins` "
        f"task downloads an unpinned build again on the next run: {meta}"
    )


def test_the_census_finds_every_installer():
    """A named census, so a renamed container fails here rather than testing nothing."""
    declared = init_containers()
    missing = [name for name in INSTALLERS if name not in declared]
    assert not missing, (
        f"the rendered Deployment no longer declares: {missing}. It has: {sorted(declared)}"
    )


@pytest.mark.parametrize("container", INSTALLERS)
def test_an_installer_sweeps_a_newer_sibling_it_did_not_install(container, tmp_path):
    _assert_pin_holds(script(container), tmp_path)


@pytest.mark.parametrize("container", INSTALLERS)
def test_an_installer_turns_auto_update_off_on_every_start(container, tmp_path):
    _assert_auto_update_off(script(container), tmp_path)


def test_the_sweep_guard_rejects_an_installer_that_exits_early(tmp_path):
    """Red proof: an early exit before the sweep must fail the check."""
    installer = script("install-media-cleaner")
    early = '    print("media-cleaner " + VERSION + " already installed")'
    assert early in installer, "fixture drift: the already-installed branch moved"
    with pytest.raises(AssertionError):
        _assert_pin_holds(
            installer.replace(early, early + "\n    raise SystemExit(0)", 1), tmp_path
        )


def test_the_auto_update_guard_rejects_an_installer_that_leaves_the_flag_alone(
    tmp_path,
):
    """Red proof: an installer that writes no flag must fail the check."""
    installer = script("install-media-cleaner")
    victim = '        meta["autoUpdate"] = False'
    assert victim in installer, "fixture drift: the autoUpdate pin moved"
    with pytest.raises(AssertionError):
        _assert_auto_update_off(installer.replace(victim, "        pass", 1), tmp_path)
