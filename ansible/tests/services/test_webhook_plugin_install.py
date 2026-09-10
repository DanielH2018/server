#!/usr/bin/env python3
"""The Webhook plugin install step must exist, be checksum-pinned, and be loadable.

Third of jellyfin's plugin installers, and it repeats the hazard the other two record:
Jellyfin's loader refuses a plugin whose `targetAbi` is newer than the running server and says
nothing about it — the directory sits on disk, `GET /Plugins` omits it, the rollout is green.

The trap is sharper here than for the siblings. Webhook's own release line moved to Jellyfin
12: version 22.0.0.0 (2026-09-08) declares `targetAbi` 12.0.0.0, so "the newest Webhook" is the
wrong one while `jellyfin_k8s_image` is a 10.11 build. 21.0.0.0 declares 10.11.8.0 and is the
newest that loads.

Unlike Intro Skipper, this release ships its own `meta.json`, so nothing writes one — the
directory name comes from the manifest's `"name": "Webhook"`.

Run: uv run pytest ansible/tests/services/test_webhook_plugin_install.py
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
    """Both tuples zero-padded, so a prefix tie does not decide the comparison.

    `(10, 11, 8, 0) <= (10, 11, 11)` compares a four-part targetAbi against a three-part image
    tag; without padding the longer tuple wins ties and the pin that is correct fails.
    """
    width = max(len(left), len(right))
    return left + (0,) * (width - len(left)), right + (0,) * (width - len(right))


def test_the_release_url_carries_the_pinned_version():
    """A bump that misses the URL installs the OLD build under the NEW marker.

    The install marker is the directory name, which carries `jellyfin_k8s_webhook_version` —
    so the two drifting apart latches a build that was never downloaded, and nothing
    reconciles it because the installer's own guard is satisfied.
    """
    defaults = _defaults()
    version = defaults["jellyfin_k8s_webhook_version"]
    url = defaults["jellyfin_k8s_webhook_url"]

    assert version in url, (
        f"jellyfin_k8s_webhook_version is {version!r} but jellyfin_k8s_webhook_url does not "
        f"contain it: {url}"
    )


def test_the_plugin_target_abi_does_not_exceed_the_server():
    """The constraint the pin exists to satisfy, checked rather than remembered."""
    defaults = _defaults()
    target_abi = _version_tuple(
        defaults["jellyfin_k8s_webhook_target_abi"], "jellyfin_k8s_webhook_target_abi"
    )
    image = defaults["jellyfin_k8s_image"]
    server = _version_tuple(image.rsplit(":", 1)[-1], "the jellyfin image tag")

    abi, srv = _padded(target_abi, server)
    assert abi <= srv, (
        f"Webhook {defaults['jellyfin_k8s_webhook_version']} targets Jellyfin "
        f"{defaults['jellyfin_k8s_webhook_target_abi']}, but jellyfin_k8s_image is {image}.\n"
        f"Jellyfin's loader rejects a plugin built for a newer server, silently — the rollout "
        f"stays green and the plugin never appears in GET /Plugins. Webhook 22.0.0.0 and up "
        f"target 12.0.0.0; take the newest release whose targetAbi the image satisfies."
    )


def _assert_install_step(template: str, version: str) -> None:
    """Everything the rendered deployment must carry for the install to work.

    Factored out so the same assertions can run against a MUTATED template. A guard that is
    only ever handed the real file is a guard nobody has seen fail.
    """
    assert "- name: install-webhook" in template, (
        "the deployment no longer declares the install-webhook init container"
    )
    assert "{{ jellyfin_k8s_webhook_md5 }}" in template, (
        "the install-webhook init container no longer templates jellyfin_k8s_webhook_md5 — "
        "an unpinned download is whatever repo.jellyfin.org happens to serve today"
    )
    assert "if got != WANT_MD5:" in template, (
        "the install-webhook init container downloads the archive but no longer COMPARES its "
        "digest — the pin is present and inert"
    )
    assert "{{ jellyfin_k8s_webhook_version }}" in template, (
        "the install-webhook init container no longer templates jellyfin_k8s_webhook_version "
        "— the install marker would stop tracking the pin"
    )
    assert version not in template, (
        f"the version {version!r} is written literally into {DEPLOYMENT.name}. Take it from "
        f"jellyfin_k8s_webhook_version instead."
    )
    assert 'PLUGINS = Path("/config/data/plugins")' in template, (
        "the install-webhook init container no longer targets /config/data/plugins. Jellyfin "
        "scans only that directory — installing anywhere else reports success and leaves the "
        "plugin unloaded, behind a green rollout."
    )
    assert 'PLUGIN_DIR = PLUGINS / ("Webhook_" + VERSION)' in template, (
        "the plugin directory no longer follows Jellyfin's <Name>_<Version> layout"
    )
    assert 'DLL = "Jellyfin.Plugin.Webhook.dll"' in template, (
        "the install-webhook init container no longer checks for the plugin's own DLL. The "
        "release ships seven bundled dependency DLLs beside it, so an extraction that "
        "succeeds proves nothing about whether Jellyfin can load anything."
    )


def test_the_rendered_deployment_carries_the_pinned_install_step():
    _assert_install_step(
        DEPLOYMENT.read_text(), _defaults()["jellyfin_k8s_webhook_version"]
    )


@pytest.mark.parametrize(
    ("what", "victim"),
    [
        ("the checksum comparison", "if got != WANT_MD5:"),
        ("the checksum itself", "{{ jellyfin_k8s_webhook_md5 }}"),
        ("the whole init container", "- name: install-webhook"),
        ("the plugin DLL check", 'DLL = "Jellyfin.Plugin.Webhook.dll"'),
        (
            "the plugin directory layout",
            'PLUGIN_DIR = PLUGINS / ("Webhook_" + VERSION)',
        ),
    ],
)
def test_the_guard_rejects_a_template_missing_the_step(what, victim):
    """The red half. Each removal above is a real way this install goes quietly wrong."""
    mutated = DEPLOYMENT.read_text().replace(victim, "")
    assert victim not in mutated, (
        f"the mutation for {what} matched nothing — fix the fixture"
    )

    with pytest.raises(AssertionError):
        _assert_install_step(mutated, _defaults()["jellyfin_k8s_webhook_version"])


def test_all_three_plugin_installers_are_still_present():
    """Non-vacuity, and the lockstep rule stated where a rename breaks it.

    The role's CLAUDE.md pins `jellyfin_k8s_image` against the targetAbi of every installed
    plugin. That rule is only checkable if the census of installers is known, and each of the
    three is guarded by its own file — so a fourth added without a guard, or one renamed out
    from under its guard, is exactly what this asserts against.
    """
    template = DEPLOYMENT.read_text()
    installers = set(re.findall(r"- name: (install-[a-z-]+)", template))

    assert installers == {
        "install-ani-sync",
        "install-intro-skipper",
        "install-webhook",
    }, (
        f"jellyfin's plugin installers are {sorted(installers)}. Each one pins the image "
        f"through its targetAbi and each is guarded by its own test file — add or rename one "
        f"and this census must move with it."
    )
