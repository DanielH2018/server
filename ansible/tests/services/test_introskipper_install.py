#!/usr/bin/env python3
"""The Intro Skipper install step must exist, be checksum-pinned, and be loadable.

Jellyfin's plugin loader refuses a plugin whose `targetAbi` is newer than the running server,
and it does so quietly — the directory sits on disk, `GET /Plugins` does not list it, the
rollout is green and the health gate passes. `test_anisync_pin_matches_server.py` records that
hazard for the sibling plugin; this file is the same memory for Intro Skipper, plus the two
things that install does differently.

The first difference is the release line. Upstream maintains one line per JELLYFIN version and
tags them `10.11/v1.10.11.23` and `12.0/v12.0.2.8` in the same repository, so "the latest
release" is routinely the wrong one. The second is the checksum: this pin is a sha256 (GitHub's
own asset digest), where ani-sync pins the MD5 its manifest publishes.

The install step is read out of the RENDERED Deployment (`_jellyfin_plugins`), so each assertion
is about a value the pod receives rather than a line of `deployment.yaml.j2`. The pin reaching the
container is proved by rendering the role again with the version flipped, which is the half a
single render cannot show: a literal and a templated expression render identically.

Run: uv run pytest ansible/tests/services/test_introskipper_install.py
"""

import json
import re

import pytest

from _helpers import ANSIBLE, REPO, load_defaults
from _jellyfin_plugins import (
    PLUGIN_ROOT,
    compares_against,
    plugin_constants,
    init_containers,
    script,
    without_assignment,
)

JELLYFIN = ANSIBLE / "roles" / "k8s" / "jellyfin"
RENOVATE = REPO / "renovate.json"

INSTALLER = "install-intro-skipper"
PLUGIN_NAME = "Intro Skipper"


def _introskipper_manager() -> dict:
    """The customManager in renovate.json that tracks this plugin."""
    managers = json.loads(RENOVATE.read_text())["customManagers"]
    manager = next(
        (
            m
            for m in managers
            if m.get("depNameTemplate") == "intro-skipper/intro-skipper"
        ),
        None,
    )
    assert manager, (
        "renovate.json no longer carries a customManager for intro-skipper/intro-skipper — "
        "the plugin pin ages with no update signal at all"
    )
    return manager


def _assert_anchor_tracks_the_image(manager: dict, image: str) -> None:
    """The manager's release-line anchor must name the Jellyfin minor the image runs."""
    server = _version_tuple(image.rsplit(":", 1)[-1], "the jellyfin image tag")
    anchor = manager["extractVersionTemplate"]

    assert f"{server[0]}\\.{server[1]}" in anchor, (
        f"jellyfin_k8s_image is a {server[0]}.{server[1]} build ({image}), but the "
        f"intro-skipper Renovate manager anchors extractVersionTemplate to {anchor!r}.\n"
        f"Raise the anchor with the image, or the manager tracks a release line upstream has "
        f"moved off — it then matches nothing and offers nothing, which reads exactly like a "
        f"plugin with no updates available."
    )


LEADING_VERSION = re.compile(r"^(\d+(?:\.\d+)*)")


def _version_tuple(text: str, what: str) -> tuple[int, ...]:
    match = LEADING_VERSION.match(text)
    assert match, f"{what} does not start with a dotted version: {text!r}"
    return tuple(int(part) for part in match.group(1).split("."))


def _padded(left: tuple[int, ...], right: tuple[int, ...]) -> tuple[tuple, tuple]:
    """Both tuples zero-padded to the same length.

    Not cosmetic. The plugin declares a four-part targetAbi (`10.11.11.0`) while the image tag
    carries three parts (`10.11.11`), and `(10, 11, 11, 0) <= (10, 11, 11)` is False in Python —
    the longer tuple wins a prefix tie. Comparing them unpadded fails the pin that is correct.
    """
    width = max(len(left), len(right))
    return (
        left + (0,) * (width - len(left)),
        right + (0,) * (width - len(right)),
    )


def test_the_release_url_carries_the_pinned_version():
    """A version bump that misses the URL installs the OLD build under the NEW marker.

    The install marker is the directory name, which carries
    `jellyfin_k8s_introskipper_version` — so the two drifting apart latches a build that was
    never downloaded, and nothing reconciles it because the guard is satisfied.
    """
    defaults = load_defaults(JELLYFIN)
    version = defaults["jellyfin_k8s_introskipper_version"]
    url = defaults["jellyfin_k8s_introskipper_url"]

    assert version in url, (
        f"jellyfin_k8s_introskipper_version is {version!r} but "
        f"jellyfin_k8s_introskipper_url does not contain it: {url}"
    )


def test_the_release_comes_from_the_jellyfin_line_that_is_deployed():
    """Upstream ships a 10.11 line and a 12.0 line from one repository, in parallel.

    Renovate ranks `12.0/v12.0.2.8` above `10.11/v1.10.11.23`, and the download URL's path
    segment is the only place the line is written down. A 12.0 asset against a 10.11 server is
    the silent-rejection case above.
    """
    defaults = load_defaults(JELLYFIN)
    url = defaults["jellyfin_k8s_introskipper_url"]
    image = defaults["jellyfin_k8s_image"]

    server = _version_tuple(image.rsplit(":", 1)[-1], "the jellyfin image tag")
    line = f"{server[0]}.{server[1]}"

    assert f"/releases/download/{line}/" in url, (
        f"jellyfin_k8s_image is a {line} build, but jellyfin_k8s_introskipper_url does not "
        f"come from the {line} release line: {url}\nUpstream tags per Jellyfin version "
        f"(`{line}/v...`); an asset from another line declares a targetAbi Jellyfin's loader "
        f"rejects without logging a deploy failure."
    )


def test_the_plugin_target_abi_does_not_exceed_the_server():
    """The constraint the pin exists to satisfy, checked rather than remembered."""
    defaults = load_defaults(JELLYFIN)
    target_abi = _version_tuple(
        defaults["jellyfin_k8s_introskipper_target_abi"],
        "jellyfin_k8s_introskipper_target_abi",
    )
    image = defaults["jellyfin_k8s_image"]
    server = _version_tuple(image.rsplit(":", 1)[-1], "the jellyfin image tag")

    abi, srv = _padded(target_abi, server)
    assert abi <= srv, (
        f"Intro Skipper {defaults['jellyfin_k8s_introskipper_version']} targets Jellyfin "
        f"{defaults['jellyfin_k8s_introskipper_target_abi']}, but jellyfin_k8s_image is "
        f"{image}.\nJellyfin's loader rejects a plugin built for a newer server, silently — "
        f"the rollout stays green and the plugin never appears in GET /Plugins."
    )


def _assert_install_step(installer: str, defaults: dict) -> None:
    """Everything the installer the pod runs must carry for the install to work.

    Factored out of the test below so the same assertions can be run against a MUTATED script. A
    guard that is only ever handed the real one is a guard nobody has seen fail.

    Args:
        installer: the Python the `install-intro-skipper` init container runs.
        defaults: the jellyfin role's defaults, holding the pins the script must agree with.
    """
    consts = plugin_constants(installer)
    version = defaults["jellyfin_k8s_introskipper_version"]

    assert consts.get("WANT_SHA256") == defaults["jellyfin_k8s_introskipper_sha256"], (
        f"the installer checks sha256 {consts.get('WANT_SHA256')!r}, but the role pins "
        f"{defaults['jellyfin_k8s_introskipper_sha256']!r} — an unpinned download is whatever "
        f"the release assets happen to hold today"
    )
    assert compares_against(installer, "WANT_SHA256"), (
        "the installer downloads the archive but no longer COMPARES its digest against "
        "WANT_SHA256 — the pin is present and inert"
    )
    assert consts.get("VERSION") == version, (
        f"the installer installs {consts.get('VERSION')!r} while the role pins {version!r}. "
        f"The install marker is the directory name, so the two drifting apart latches a build "
        f"that was never downloaded."
    )
    assert consts.get("PLUGINS") == PLUGIN_ROOT, (
        f"the installer targets {consts.get('PLUGINS')!r}, not {PLUGIN_ROOT!r}. Jellyfin scans "
        f"only that directory — installing anywhere else reports success and leaves the plugin "
        f"unloaded, behind a green rollout."
    )
    assert consts.get("PLUGIN_DIR") == f"{PLUGIN_ROOT}/{PLUGIN_NAME}_{version}", (
        f"the plugin directory is {consts.get('PLUGIN_DIR')!r}, not Jellyfin's "
        f"<Name>_<Version> layout under {PLUGIN_ROOT}"
    )
    assert '(staged / "meta.json").write_text' in installer, (
        "the installer no longer writes meta.json. This release ships the DLL alone, and "
        "without a manifest Jellyfin invents one — deriving the plugin id from an MD5 of the "
        "directory name instead of its real GUID."
    )


def test_the_deployment_declares_the_installer():
    """Non-vacuity: every assertion below reads one init container out of the render."""
    containers = init_containers()
    assert INSTALLER in containers, (
        f"the rendered jellyfin Deployment no longer declares {INSTALLER}, so Intro Skipper is "
        f"installed by nothing. It has: {sorted(containers)}"
    )


def test_the_rendered_installer_carries_the_pinned_install_step():
    _assert_install_step(script(INSTALLER), load_defaults(JELLYFIN))


def test_the_install_marker_follows_the_pin_rather_than_a_literal():
    """A version written literally in the template is a second place to forget to bump.

    A single render cannot see the difference — a literal and `{{ ... }}` produce the same text —
    so the role is rendered again with the pin flipped. The marker path, the download and the log
    lines all have to name one version, and this is what keeps that true.
    """
    bumped = plugin_constants(
        script(INSTALLER, {"jellyfin_k8s_introskipper_version": "9.9.9.9"})
    )
    assert bumped.get("VERSION") == "9.9.9.9", (
        f"the installer still installs {bumped.get('VERSION')!r} after "
        f"jellyfin_k8s_introskipper_version was flipped to 9.9.9.9, so the version is written "
        f"into the template rather than taken from the pin"
    )
    assert bumped.get("PLUGIN_DIR") == f"{PLUGIN_ROOT}/{PLUGIN_NAME}_9.9.9.9", (
        f"the install marker is {bumped.get('PLUGIN_DIR')!r}, which does not follow the pin — a "
        f"bump would record a version that was never downloaded"
    )


@pytest.mark.parametrize(
    ("what", "constant"),
    [
        ("the checksum itself", "WANT_SHA256"),
        ("the version the marker names", "VERSION"),
        ("the plugins directory", "PLUGINS"),
    ],
)
def test_the_guard_rejects_an_installer_missing_a_pin(what, constant):
    """The red half, by perturbation: each constant removed is a real way this goes wrong."""
    with pytest.raises(AssertionError):
        _assert_install_step(
            without_assignment(script(INSTALLER), constant), load_defaults(JELLYFIN)
        )


@pytest.mark.parametrize(
    ("what", "victim", "replacement"),
    [
        ("the checksum comparison", "got != WANT_SHA256", "got != got"),
        ("the meta.json write", '(staged / "meta.json").write_text', "str"),
    ],
)
def test_the_guard_rejects_an_installer_that_stopped_acting_on_a_pin(
    what, victim, replacement
):
    """The red half for the two behaviours no constant carries.

    Both are perturbations of the SCRIPT the pod runs, not of `deployment.yaml.j2`: the pin can
    be present and never acted on, which no constant assertion can see.
    """
    installer = script(INSTALLER)
    assert victim in installer, (
        f"the mutation for {what} matched nothing — fix the fixture"
    )

    with pytest.raises(AssertionError):
        _assert_install_step(
            installer.replace(victim, replacement), load_defaults(JELLYFIN)
        )


def test_renovate_tracks_the_jellyfin_line_that_is_deployed():
    """The Renovate manager's release-line anchor has to move with the image.

    `extractVersionTemplate` is anchored to one Jellyfin line (`^10\\.11/v...`) so the
    datasource cannot offer a 12.0 build against a 10.11 server. That anchor is hand-written in
    renovate.json while the line itself is decided by `jellyfin_k8s_image` two files away, so a
    Jellyfin minor bump that updates the pin and the URL leaves the manager anchored to a line
    upstream has stopped releasing on. It then matches nothing, offers nothing, and reads
    exactly like a plugin with no updates available — green and inert, which is the failure the
    manager's own description says it was written to avoid.
    """
    _assert_anchor_tracks_the_image(
        _introskipper_manager(), load_defaults(JELLYFIN)["jellyfin_k8s_image"]
    )


def test_the_anchor_guard_rejects_a_manager_left_on_the_old_line():
    """The red half: the guard must fail on the drift it exists to catch."""
    stale = dict(
        _introskipper_manager(), extractVersionTemplate="^9\\.9/v(?<version>.+)$"
    )

    with pytest.raises(AssertionError):
        _assert_anchor_tracks_the_image(
            stale, load_defaults(JELLYFIN)["jellyfin_k8s_image"]
        )
