#!/usr/bin/env python3
"""Jellyfin's plugin allowlist sweep keeps exactly what the role installs, and nothing else.

`sweep-unlisted-plugins` walks the top-level entries of /config/data/plugins and removes a
`<Name>_<Version>` directory unless `<Name>` is in its KEEP tuple. Three things decide whether
that is safe:

- **KEEP equals the set the installers write.** A name missing from KEEP makes the sweep remove
  a plugin the role installs; a name KEEP carries that no installer writes lets an unmanaged
  plugin survive. Both sides come out of the RENDERED Deployment (`_jellyfin_plugins`) — KEEP and
  each installer's `PLUGIN_DIR` are read off the scripts the pod runs — so a sixth installer
  fails here rather than on the PVC.
- **`configurations/` is never swept.** It holds every plugin's settings, and git holds none of
  them.
- **It runs before the installers**, so a KEEP gap costs a re-download, not the plugin.

Run: uv run pytest ansible/tests/services/test_jellyfin_plugin_allowlist.py
"""

import sys

import pytest

from _jellyfin_plugins import (
    INSTALLERS,
    PLUGIN_ROOT,
    SWEEP,
    plugin_constants,
    init_containers,
    script,
    without_assignment,
)
from lib.proc_testing import run


def _installed_names() -> set[str]:
    """The plugin NAME each installer writes, from its rendered `PLUGIN_DIR`.

    `PLUGIN_DIR` folds to `<root>/<Name>_<version>`, and the sweep matches on the name alone, so
    the version is cut back off here.
    """
    names = set()
    for installer in INSTALLERS:
        consts = plugin_constants(script(installer))
        plugin_dir = consts.get("PLUGIN_DIR")
        assert isinstance(plugin_dir, str) and plugin_dir.startswith(
            f"{PLUGIN_ROOT}/"
        ), (
            f"{installer} installs to {plugin_dir!r}, not a directory under {PLUGIN_ROOT}"
        )
        names.add(plugin_dir.rsplit("/", 1)[-1].rsplit("_", 1)[0])
    return names


def _keep(sweep: str) -> tuple[str, ...]:
    keep = plugin_constants(sweep).get("KEEP")
    assert isinstance(keep, tuple), "the sweep no longer assigns a KEEP tuple"
    return keep


def _assert_keep_matches_installers(sweep: str, installed: set[str]) -> None:
    keep = set(_keep(sweep))
    assert keep == installed, (
        f"KEEP and the installers disagree. Installed but not kept: {installed - keep}; the "
        f"sweep would remove those on every start. Kept but not installed: {keep - installed}; "
        f"those are plugins no pin describes, surviving the sweep."
    )


def test_the_installer_census_finds_every_installer():
    """A pattern-found subject must find a named member, or it can pass over an empty set."""
    installed = _installed_names()
    assert "Media Cleaner" in installed and len(installed) == len(INSTALLERS), installed


def test_keep_names_every_installer_and_nothing_else():
    _assert_keep_matches_installers(script(SWEEP), _installed_names())


@pytest.mark.parametrize(
    ("what", "drifted"),
    [
        ("an installed plugin dropped from KEEP", {"Media Cleaner"}),
        ("an uninstalled plugin added to KEEP", {"Trakt"}),
    ],
)
def test_the_guard_rejects_a_keep_that_drifts(what, drifted):
    """Red proof: KEEP out of step with the installers must fail, in both directions.

    The drift is applied to the INSTALLED set rather than to the sweep's source, which makes the
    perturbation symmetric: dropping a name is a plugin the sweep would remove, adding one is a
    plugin that survives it.
    """
    installed = _installed_names() ^ drifted
    with pytest.raises(AssertionError):
        _assert_keep_matches_installers(script(SWEEP), installed)


def test_the_sweep_runs_before_every_installer():
    order = list(init_containers())
    installers = [order.index(name) for name in INSTALLERS]
    assert order.index(SWEEP) < min(installers), (
        f"{SWEEP} runs after an installer. A KEEP gap then removes a plugin the installer just "
        f"wrote, on every start, instead of costing one re-download. Order: {order}"
    )


def test_the_keep_reader_rejects_a_sweep_that_assigns_no_keep():
    """Red proof for the reader itself: an empty KEEP would pass every comparison above."""
    with pytest.raises(AssertionError):
        _keep(without_assignment(script(SWEEP), "KEEP"))


def _run(sweep: str, tmp_path) -> str:
    """The sweep, run for real against `tmp_path` instead of the PVC."""
    assert PLUGIN_ROOT in sweep, (
        "fixture drift: the sweep no longer names its directory"
    )
    done = run(
        [sys.executable, "-c", sweep.replace(PLUGIN_ROOT, str(tmp_path))], check=True
    )
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
    _assert_report(_run(script(SWEEP), plugins))
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
    settings = plugin_constants(script(SWEEP))["SETTINGS"]
    swept = script(SWEEP).replace(
        f'SETTINGS = "{settings}"', 'SETTINGS = "something-else"'
    )
    with pytest.raises(AssertionError):
        _assert_report(_run(swept, plugins))


def test_a_fresh_volume_is_not_an_error(tmp_path):
    assert "nothing to sweep" in _run(script(SWEEP), tmp_path / "absent")
