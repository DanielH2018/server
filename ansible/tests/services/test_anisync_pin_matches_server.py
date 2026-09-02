#!/usr/bin/env python3
"""The pinned jellyfin-ani-sync build must be loadable by the pinned Jellyfin image.

Jellyfin's plugin loader compares a plugin's `targetAbi` against the running server and
refuses anything built for a NEWER server. The rejection is quiet: the plugin directory sits
on disk, `GET /Plugins` does not list it, and Jellyfin starts normally. Nothing about the
deploy goes red — the rollout succeeds, the health gate passes, and the only symptom is that
watch status stops reaching AniList.

That is a live hazard rather than a hypothetical one. On 2026-08-25 the plugin's current
release was 4.4.0.0 with `targetAbi` 10.11.11.0, while this role pinned Jellyfin at 10.11.10 —
so the obvious "just take the latest" bump installs a plugin that never loads. The role pins
4.1.0.0 (`targetAbi` 10.11.6.0) for exactly that reason, and the pin is only safe while
someone remembers why. This test is that memory.

Both halves are readable offline. The release asset's filename leads with the `targetAbi` it
was built for (`10.11.6.-.ani-sync_4.1.0.0.zip`), and the image tag leads with the server
version (`10.11.10ubu2404-ls35`), so the comparison needs no network call and no manifest
fetch.

Run: uv run pytest ansible/tests/services/test_anisync_pin_matches_server.py
"""

import re

import yaml
from _helpers import ANSIBLE

DEFAULTS = ANSIBLE / "roles" / "k8s" / "jellyfin" / "defaults" / "main.yml"
DEPLOYMENT = ANSIBLE / "roles" / "k8s" / "jellyfin" / "templates" / "deployment.yaml.j2"

# Leading dotted version of a string: "10.11.6.-.ani-sync_4.1.0.0.zip" -> "10.11.6",
# "10.11.10ubu2404-ls35" -> "10.11.10".
LEADING_VERSION = re.compile(r"^(\d+(?:\.\d+)*)")


def _defaults() -> dict:
    return yaml.safe_load(DEFAULTS.read_text())


def _version_tuple(text: str, what: str) -> tuple[int, ...]:
    match = LEADING_VERSION.match(text)
    assert match, f"{what} does not start with a dotted version: {text!r}"
    return tuple(int(part) for part in match.group(1).split("."))


def test_the_release_url_carries_the_pinned_version():
    """A version bump that misses the URL installs the OLD build under the NEW marker.

    The marker file is named for `jellyfin_k8s_anisync_version`, so the two drifting apart is
    worse than either being wrong alone: the init container records 4.2.0.0 as installed,
    skips every subsequent run, and 4.1.0.0 is what is actually on disk. Nothing ever
    reconciles it, because the guard is satisfied.
    """
    defaults = _defaults()
    version = defaults["jellyfin_k8s_anisync_version"]
    url = defaults["jellyfin_k8s_anisync_url"]

    assert version in url, (
        f"jellyfin_k8s_anisync_version is {version!r} but jellyfin_k8s_anisync_url does not "
        f"contain it: {url}\nBump both together — the install marker is named for the "
        f"version and would latch a build that was never downloaded."
    )


def test_the_plugin_target_abi_does_not_exceed_the_server():
    """The constraint the pin exists to satisfy, checked rather than remembered."""
    defaults = _defaults()
    url = defaults["jellyfin_k8s_anisync_url"]
    image = defaults["jellyfin_k8s_image"]

    asset = url.rsplit("/", 1)[-1]
    target_abi = _version_tuple(asset, "the release asset filename")

    tag = image.rsplit(":", 1)[-1]
    server = _version_tuple(tag, "the jellyfin image tag")

    assert target_abi <= server, (
        f"jellyfin-ani-sync asset {asset!r} targets Jellyfin "
        f"{'.'.join(map(str, target_abi))}, but jellyfin_k8s_image is "
        f"{'.'.join(map(str, server))} ({image}).\n"
        f"Jellyfin's loader rejects a plugin whose targetAbi is newer than the server, and "
        f"it does so silently: the rollout stays green and the plugin never appears in "
        f"GET /Plugins. Raise jellyfin_k8s_image first, or pin an older plugin release."
    )


def test_the_init_container_reads_the_version_from_the_variable():
    """A literal version in the template is a second place to forget to bump.

    The marker path, the log lines and the download all have to name one version. Templating
    them from `jellyfin_k8s_anisync_version` is what keeps that true; hardcoding the string
    anywhere in the script reintroduces the drift the first test guards against.
    """
    template = DEPLOYMENT.read_text()
    version = _defaults()["jellyfin_k8s_anisync_version"]

    assert "{{ jellyfin_k8s_anisync_version }}" in template, (
        "the install-ani-sync init container no longer templates "
        "jellyfin_k8s_anisync_version — the marker file would stop tracking the pin"
    )
    assert version not in template, (
        f"the version {version!r} is written literally into {DEPLOYMENT.name}. Take it from "
        f"jellyfin_k8s_anisync_version instead, so a bump in defaults/main.yml reaches every "
        f"place that names it."
    )


def test_the_plugin_lands_where_jellyfin_actually_scans():
    """Installing to /config/plugins succeeds and does nothing. Found the hard way.

    The 2026-08-25 first deploy installed cleanly — the init container logged
    "installed ani-sync 4.1.0.0", the files were on disk, the rollout was green and
    `probe.py health jellyfin` passed — and `GET /Plugins` never listed Ani-Sync. Jellyfin
    scans `/config/data/plugins`, and every plugin it loads names a path under there:

        Loaded assembly SSO-Auth ... from /config/data/plugins/SSO Authentication_4.0.0.4/SSO-Auth.dll

    Nothing about that failure is visible from the deploy side, which is why it is pinned here
    rather than left to a comment. The directory layout is the second half: Jellyfin's own
    installer writes `<Name>_<Version>`, and `Ani-Sync` is meta.json's name, not ours.
    """
    template = DEPLOYMENT.read_text()

    assert 'PLUGINS = Path("/config/data/plugins")' in template, (
        "the install-ani-sync init container no longer targets /config/data/plugins. "
        "Jellyfin scans only that directory — installing anywhere else reports success and "
        "leaves the plugin unloaded, with a green rollout and nothing in GET /Plugins."
    )
    assert 'PLUGIN_DIR = PLUGINS / ("Ani-Sync_" + VERSION)' in template, (
        "the plugin directory no longer follows Jellyfin's <Name>_<Version> layout. "
        "'Ani-Sync' is the name meta.json declares; the sibling plugins on this volume use "
        "the same shape."
    )
