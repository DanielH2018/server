#!/usr/bin/env python3
"""The pinned jellyfin-ani-sync build must be loadable by the pinned Jellyfin image.

Jellyfin's plugin loader compares a plugin's `targetAbi` against the running server and
refuses anything built for a NEWER server. The rejection is quiet: the plugin directory sits
on disk, `GET /Plugins` does not list it, and Jellyfin starts normally. Nothing about the
deploy goes red — the rollout succeeds, the health gate passes, and the only symptom is that
watch status stops reaching AniList.

The hazard is live: a release can target a newer ABI than the pinned image (4.4.0.0 targets
10.11.11.0 while the role pins Jellyfin at 10.11.10), so the obvious "just take the latest"
bump installs a plugin that never loads. The role pins 4.1.0.0 (`targetAbi` 10.11.6.0) for that
reason. This test checks the constraint the pin exists to satisfy.

Both halves are readable offline. The release asset's filename leads with the `targetAbi` it
was built for (`10.11.6.-.ani-sync_4.1.0.0.zip`), and the image tag leads with the server
version (`10.11.10ubu2404-ls35`), so the comparison needs no network call and no manifest
fetch.

What the install step itself does is read out of the RENDERED Deployment (`_jellyfin_plugins`),
so an assertion names the value the pod receives rather than a line of `deployment.yaml.j2`.

Run: uv run pytest ansible/tests/services/test_anisync_pin_matches_server.py
"""

from _helpers import ANSIBLE, image_tag, load_defaults
from _jellyfin_plugins import PLUGIN_ROOT, plugin_constants, script, version_tuple

JELLYFIN = ANSIBLE / "roles" / "k8s" / "jellyfin"

INSTALLER = "install-ani-sync"
PLUGIN_NAME = "Ani-Sync"


def test_the_release_url_carries_the_pinned_version():
    """A version bump that misses the URL installs the OLD build under the NEW marker.

    The marker file is named for `jellyfin_k8s_anisync_version`, so the two drifting apart is
    worse than either being wrong alone: the init container records 4.2.0.0 as installed,
    skips every subsequent run, and 4.1.0.0 is what is actually on disk. Nothing ever
    reconciles it, because the guard is satisfied.
    """
    defaults = load_defaults(JELLYFIN)
    version = defaults["jellyfin_k8s_anisync_version"]
    url = defaults["jellyfin_k8s_anisync_url"]

    assert version in url, (
        f"jellyfin_k8s_anisync_version is {version!r} but jellyfin_k8s_anisync_url does not "
        f"contain it: {url}\nBump both together — the install marker is named for the "
        f"version and would latch a build that was never downloaded."
    )


def test_the_plugin_target_abi_does_not_exceed_the_server():
    """The constraint the pin exists to satisfy, checked rather than remembered."""
    defaults = load_defaults(JELLYFIN)
    url = defaults["jellyfin_k8s_anisync_url"]
    image = defaults["jellyfin_k8s_image"]

    asset = url.rsplit("/", 1)[-1]
    target_abi = version_tuple(asset, "the release asset filename")

    tag = image_tag(image)
    server = version_tuple(tag, "the jellyfin image tag")

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

    Proved by rendering the role twice, because a single render cannot tell a literal from a
    templated expression: the installer the pod runs must name the pin, and must follow it when
    the pin moves.
    """
    version = load_defaults(JELLYFIN)["jellyfin_k8s_anisync_version"]

    assert plugin_constants(script(INSTALLER)).get("VERSION") == version, (
        f"the install-ani-sync init container installs "
        f"{plugin_constants(script(INSTALLER)).get('VERSION')!r} while the role pins {version!r} — the "
        f"marker file would stop tracking the pin"
    )
    bumped = plugin_constants(
        script(INSTALLER, {"jellyfin_k8s_anisync_version": "9.9.9.9"})
    )
    assert bumped.get("VERSION") == "9.9.9.9", (
        "the version is written literally into deployment.yaml.j2: flipping "
        "jellyfin_k8s_anisync_version left the installer installing the old build. Take it from "
        "the variable, so a bump in defaults/main.yml reaches every place that names it."
    )


def test_the_plugin_lands_where_jellyfin_actually_scans():
    """Installing to /config/plugins succeeds and does nothing.

    An install there leaves the files on disk, a green rollout and a passing
    `probe.py health jellyfin`, yet `GET /Plugins` never lists Ani-Sync. Jellyfin
    scans `/config/data/plugins`, and every plugin it loads names a path under there:

        Loaded assembly SSO-Auth ... from /config/data/plugins/SSO Authentication_4.0.0.4/SSO-Auth.dll

    Nothing about that failure is visible from the deploy side, which is why it is pinned here
    rather than left to a comment. The directory layout is the second half: Jellyfin's own
    installer writes `<Name>_<Version>`, and `Ani-Sync` is meta.json's name, not ours.
    """
    consts = plugin_constants(script(INSTALLER))
    version = load_defaults(JELLYFIN)["jellyfin_k8s_anisync_version"]

    assert consts.get("PLUGINS") == PLUGIN_ROOT, (
        f"the install-ani-sync init container targets {consts.get('PLUGINS')!r}, not "
        f"{PLUGIN_ROOT!r}. Jellyfin scans only that directory — installing anywhere else "
        f"reports success and leaves the plugin unloaded, with a green rollout and nothing in "
        f"GET /Plugins."
    )
    assert consts.get("PLUGIN_DIR") == f"{PLUGIN_ROOT}/{PLUGIN_NAME}_{version}", (
        f"the plugin directory is {consts.get('PLUGIN_DIR')!r}, not Jellyfin's "
        f"<Name>_<Version> layout. '{PLUGIN_NAME}' is the name meta.json declares; the sibling "
        f"plugins on this volume use the same shape."
    )
