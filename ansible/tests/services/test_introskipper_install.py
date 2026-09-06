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

Run: uv run pytest ansible/tests/services/test_introskipper_install.py
"""

import re

import pytest

from lib import yaml_fast
from _helpers import ANSIBLE

DEFAULTS = ANSIBLE / "roles" / "k8s" / "jellyfin" / "defaults" / "main.yml"
DEPLOYMENT = ANSIBLE / "roles" / "k8s" / "jellyfin" / "templates" / "deployment.yaml.j2"

LEADING_VERSION = re.compile(r"^(\d+(?:\.\d+)*)")


def _defaults() -> dict:
    return yaml_fast.safe_load(DEFAULTS.read_text())


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
    defaults = _defaults()
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
    defaults = _defaults()
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
    defaults = _defaults()
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


def _assert_install_step(template: str, version: str) -> None:
    """Everything the rendered deployment must carry for the install to work.

    Factored out of the test below so the same assertions can be run against a MUTATED
    template. A guard that is only ever handed the real file is a guard nobody has seen fail.
    """
    assert "- name: install-intro-skipper" in template, (
        "the deployment no longer declares the install-intro-skipper init container"
    )
    assert "{{ jellyfin_k8s_introskipper_sha256 }}" in template, (
        "the install-intro-skipper init container no longer templates "
        "jellyfin_k8s_introskipper_sha256 — an unpinned download is whatever the release "
        "assets happen to hold today"
    )
    assert "if got != WANT_SHA256:" in template, (
        "the install-intro-skipper init container downloads the archive but no longer "
        "COMPARES its digest — the pin is present and inert"
    )
    assert "{{ jellyfin_k8s_introskipper_version }}" in template, (
        "the install-intro-skipper init container no longer templates "
        "jellyfin_k8s_introskipper_version — the install marker would stop tracking the pin"
    )
    assert version not in template, (
        f"the version {version!r} is written literally into {DEPLOYMENT.name}. Take it from "
        f"jellyfin_k8s_introskipper_version instead."
    )
    assert 'PLUGINS = Path("/config/data/plugins")' in template, (
        "the install-intro-skipper init container no longer targets /config/data/plugins. "
        "Jellyfin scans only that directory — installing anywhere else reports success and "
        "leaves the plugin unloaded, behind a green rollout."
    )
    assert 'PLUGIN_DIR = PLUGINS / ("Intro Skipper_" + VERSION)' in template, (
        "the plugin directory no longer follows Jellyfin's <Name>_<Version> layout"
    )
    assert '(staged / "meta.json").write_text' in template, (
        "the install-intro-skipper init container no longer writes meta.json. This release "
        "ships the DLL alone, and without a manifest Jellyfin invents one — deriving the "
        "plugin id from an MD5 of the directory name instead of its real GUID."
    )


def test_the_rendered_deployment_carries_the_pinned_install_step():
    _assert_install_step(
        DEPLOYMENT.read_text(), _defaults()["jellyfin_k8s_introskipper_version"]
    )


@pytest.mark.parametrize(
    ("what", "victim"),
    [
        ("the checksum comparison", "if got != WANT_SHA256:"),
        ("the checksum itself", "{{ jellyfin_k8s_introskipper_sha256 }}"),
        ("the whole init container", "- name: install-intro-skipper"),
        ("the meta.json write", '(staged / "meta.json").write_text'),
        ("the plugins directory", 'PLUGINS = Path("/config/data/plugins")'),
    ],
)
def test_the_guard_rejects_a_template_missing_the_step(what, victim):
    """The red half. Each removal above is a real way this install goes quietly wrong."""
    mutated = DEPLOYMENT.read_text().replace(victim, "")
    assert victim not in mutated, (
        f"the mutation for {what} matched nothing — fix the fixture"
    )

    with pytest.raises(AssertionError):
        _assert_install_step(mutated, _defaults()["jellyfin_k8s_introskipper_version"])
