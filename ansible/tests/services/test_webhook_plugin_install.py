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

import json
import re

import pytest

from lib import yaml_fast
from _helpers import ANSIBLE, REPO

DEFAULTS = ANSIBLE / "roles" / "k8s" / "jellyfin" / "defaults" / "main.yml"
DEPLOYMENT = ANSIBLE / "roles" / "k8s" / "jellyfin" / "templates" / "deployment.yaml.j2"
RENOVATE = REPO / "renovate.json"

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


def test_every_plugin_installer_is_still_present():
    """Non-vacuity, and the lockstep rule stated where a rename breaks it.

    The role's CLAUDE.md pins `jellyfin_k8s_image` against the targetAbi of every installed
    plugin. That rule is only checkable if the census of installers is known, and each one is
    guarded by its own file — so a new installer added without a guard, or one renamed out
    from under its guard, is exactly what this asserts against.

    NOT NAMED FOR A COUNT. It was `test_all_five_...` until Trakt and SSO-Auth landed (#1617,
    #1648), and a test whose name carries a number lies from the next addition on.
    """
    template = DEPLOYMENT.read_text()
    installers = set(re.findall(r"- name: (install-[a-z-]+)", template))

    assert installers == {
        "install-ani-sync",
        "install-intro-skipper",
        "install-webhook",
        "install-merge-versions",
        "install-media-cleaner",
        "install-trakt",
        "install-sso-auth",
    }, (
        f"jellyfin's plugin installers are {sorted(installers)}. Each one pins the image "
        f"through its targetAbi and each is guarded by its own test file — add or rename one "
        f"and this census must move with it."
    )


# ── Renovate coverage (#1557) ───────────────────────────────────────────────────────────────
# The pin had no update signal at all until this manager existed: renovate.json's k8s-images
# manager keys on `_image:`, so a plugin version, its URL and its MD5 were invisible to every
# manager and aged silently — which reads exactly like a plugin with no updates available.

DEP = "jellyfin/jellyfin-plugin-webhook"


def _renovate() -> dict:
    return json.loads(RENOVATE.read_text())


def _webhook_manager() -> dict:
    manager = next(
        (m for m in _renovate()["customManagers"] if m.get("depNameTemplate") == DEP),
        None,
    )
    assert manager, (
        f"renovate.json no longer carries a customManager for {DEP} — the Webhook pin ages "
        f"with no update signal at all"
    )
    return manager


def test_every_manager_pattern_still_matches_the_pinned_version():
    """A manager whose matchStrings match nothing is inert, and inert looks like up-to-date.

    Both patterns must capture the SAME major, because Renovate rewrites the URL and the
    version var in step — a rewrite that reached only one would leave the marker naming a
    build that was never downloaded.
    """
    text = DEFAULTS.read_text()
    major = _defaults()["jellyfin_k8s_webhook_version"].split(".")[0]

    for pattern in _webhook_manager()["matchStrings"]:
        found = re.findall(pattern.replace("(?<", "(?P<"), text)
        assert found, (
            f"the Webhook Renovate manager's matchString {pattern!r} matches nothing in "
            f"{DEFAULTS.name} — the manager is inert, which reads as 'no updates available'"
        )
        assert set(found) == {major}, (
            f"the Webhook Renovate manager's matchString {pattern!r} captured {sorted(set(found))} "
            f"in {DEFAULTS.name}, not the pinned major {major!r}"
        )


def test_the_manager_anchor_admits_the_pin_and_drops_the_jellyfin_12_line():
    """The anchor is what keeps 'the newest Webhook' from being offered against a 10.11 server.

    Webhook 22.0.0.0 declares targetAbi 12.0.0.0, which Jellyfin 10.11's loader rejects
    silently — the directory sits on disk, `GET /Plugins` omits it, the rollout is green. The
    upstream tags are a bare major (`v21`, `v22`), so unlike the sibling plugins the release
    line is not written into the tag path: extractVersionTemplate carries the ceiling instead,
    and it is hand-raised when jellyfin_k8s_image moves to Jellyfin 12.
    """
    anchor = re.compile(
        _webhook_manager()["extractVersionTemplate"].replace("(?<", "(?P<")
    )
    major = _defaults()["jellyfin_k8s_webhook_version"].split(".")[0]

    assert anchor.match(f"v{major}"), (
        f"the Webhook Renovate manager's extractVersionTemplate "
        f"{anchor.pattern!r} does not admit the pinned tag v{major} — the manager offers "
        f"nothing, including the release it is pinned to"
    )
    assert not anchor.match("v22"), (
        f"the Webhook Renovate manager's extractVersionTemplate {anchor.pattern!r} admits "
        f"v22. Webhook 22.0.0.0 targets Jellyfin 12.0.0.0 and the deployed 10.11 server "
        f"rejects it without logging anything. Raise this anchor only with jellyfin_k8s_image."
    )


def test_the_webhook_bump_is_never_automerged():
    """The finish is manual: the MD5 and the targetAbi come from the official manifest.

    The rule's POSITION is load-bearing, exactly as the two sibling rules' own descriptions
    record — the `ansible/roles/k8s/**` rules earlier in the array set automerge for anything
    under that path and later rules win, so a rule placed before them is silently defeated.
    """
    rules = _renovate()["packageRules"]
    index = next(
        (i for i, r in enumerate(rules) if DEP in r.get("matchPackageNames", [])),
        None,
    )
    assert index is not None, (
        f"renovate.json no longer carries a packageRule for {DEP} — a bump would join the "
        f"automerging `k8s image jellyfin` group and ship a half-finished pin"
    )
    assert rules[index].get("automerge") is False, (
        f"the {DEP} packageRule no longer sets automerge: false"
    )

    k8s_plane = [
        i
        for i, r in enumerate(rules)
        if any(p.startswith("ansible/roles/k8s/") for p in r.get("matchFileNames", []))
    ]
    assert k8s_plane, (
        "no packageRule scopes ansible/roles/k8s/** any more — this guard's premise moved, "
        "re-derive which rule would otherwise automerge the plugin pins"
    )
    assert index > max(k8s_plane), (
        f"the {DEP} packageRule sits at index {index}, before the k8s-plane rule at "
        f"{max(k8s_plane)}. Later rules win, so this one's automerge: false is defeated from "
        f"where it stands. Append it at the end of packageRules, beside its two siblings."
    )
