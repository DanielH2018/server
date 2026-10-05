#!/usr/bin/env python3
"""The Webhook plugin install step must exist, be checksum-pinned, and be loadable.

Third of jellyfin's plugin installers, and it repeats the hazard the other two record:
Jellyfin's loader refuses a plugin whose `targetAbi` is newer than the running server and says
nothing about it — the directory sits on disk, `GET /Plugins` omits it, the rollout is green.

The trap is sharper here than for the siblings. Webhook's own release line moved to Jellyfin
12: version 22.0.0.0 declares `targetAbi` 12.0.0.0, so "the newest Webhook" is the
wrong one while `jellyfin_k8s_image` is a 10.11 build. 21.0.0.0 declares 10.11.8.0 and is the
newest that loads.

Unlike Intro Skipper, this release ships its own `meta.json`, so nothing writes one — the
directory name comes from the manifest's `"name": "Webhook"`.

Run: uv run pytest ansible/tests/services/test_webhook_plugin_install.py
"""

import json
import re

import pytest

from _helpers import ANSIBLE, REPO, image_tag, load_defaults
from _jellyfin_plugins import (
    INSTALLERS,
    PLUGIN_ROOT,
    compares_against,
    init_containers,
    padded,
    plugin_constants,
    script,
    version_tuple,
    without_assignment,
)

JELLYFIN = ANSIBLE / "roles" / "k8s" / "jellyfin"
DEFAULTS = JELLYFIN / "defaults" / "main.yml"
RENOVATE = REPO / "renovate.json"

INSTALLER = "install-webhook"
PLUGIN_NAME = "Webhook"


def test_the_release_url_carries_the_pinned_version():
    """A bump that misses the URL installs the OLD build under the NEW marker.

    The install marker is the directory name, which carries `jellyfin_k8s_webhook_version` —
    so the two drifting apart latches a build that was never downloaded, and nothing
    reconciles it because the installer's own guard is satisfied.
    """
    defaults = load_defaults(JELLYFIN)
    version = defaults["jellyfin_k8s_webhook_version"]
    url = defaults["jellyfin_k8s_webhook_url"]

    assert version in url, (
        f"jellyfin_k8s_webhook_version is {version!r} but jellyfin_k8s_webhook_url does not "
        f"contain it: {url}"
    )


def test_the_plugin_target_abi_does_not_exceed_the_server():
    """The constraint the pin exists to satisfy, checked rather than remembered."""
    defaults = load_defaults(JELLYFIN)
    target_abi = version_tuple(
        defaults["jellyfin_k8s_webhook_target_abi"], "jellyfin_k8s_webhook_target_abi"
    )
    image = defaults["jellyfin_k8s_image"]
    server = version_tuple(image_tag(image), "the jellyfin image tag")

    abi, srv = padded(target_abi, server)
    assert abi <= srv, (
        f"Webhook {defaults['jellyfin_k8s_webhook_version']} targets Jellyfin "
        f"{defaults['jellyfin_k8s_webhook_target_abi']}, but jellyfin_k8s_image is {image}.\n"
        f"Jellyfin's loader rejects a plugin built for a newer server, silently — the rollout "
        f"stays green and the plugin never appears in GET /Plugins. Webhook 22.0.0.0 and up "
        f"target 12.0.0.0; take the newest release whose targetAbi the image satisfies."
    )


def _assert_install_step(installer: str, defaults: dict) -> None:
    """Everything the installer the pod runs must carry for the install to work.

    Factored out so the same assertions can run against a MUTATED script. A guard that is only
    ever handed the real one is a guard nobody has seen fail.

    Args:
        installer: the Python the `install-webhook` init container runs.
        defaults: the jellyfin role's defaults, holding the pins the script must agree with.
    """
    consts = plugin_constants(installer)
    version = defaults["jellyfin_k8s_webhook_version"]

    assert consts.get("WANT_MD5") == defaults["jellyfin_k8s_webhook_md5"], (
        f"the installer checks MD5 {consts.get('WANT_MD5')!r}, but the role pins "
        f"{defaults['jellyfin_k8s_webhook_md5']!r} — an unpinned download is whatever "
        f"repo.jellyfin.org happens to serve today"
    )
    assert compares_against(installer, "WANT_MD5"), (
        "the installer downloads the archive but no longer COMPARES its digest against "
        "WANT_MD5 — the pin is present and inert"
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
    assert consts.get("DLL") == "Jellyfin.Plugin.Webhook.dll", (
        f"the installer checks for {consts.get('DLL')!r}, not the plugin's own DLL. The release "
        f"ships seven bundled dependency DLLs beside it, so an extraction that succeeds proves "
        f"nothing about whether Jellyfin can load anything."
    )


def test_the_deployment_declares_the_installer():
    """Non-vacuity: every assertion below reads one init container out of the render."""
    containers = init_containers()
    assert INSTALLER in containers, (
        f"the rendered jellyfin Deployment no longer declares {INSTALLER}, so Webhook is "
        f"installed by nothing. It has: {sorted(containers)}"
    )


def test_the_rendered_installer_carries_the_pinned_install_step():
    _assert_install_step(script(INSTALLER), load_defaults(JELLYFIN))


def test_the_install_marker_follows_the_pin_rather_than_a_literal():
    """A version written literally in the template is a second place to forget to bump.

    A single render cannot see the difference — a literal and `{{ ... }}` produce the same text —
    so the role is rendered again with the pin flipped.
    """
    bumped = plugin_constants(
        script(INSTALLER, {"jellyfin_k8s_webhook_version": "9.9.9.9"})
    )
    assert bumped.get("VERSION") == "9.9.9.9", (
        f"the installer still installs {bumped.get('VERSION')!r} after "
        f"jellyfin_k8s_webhook_version was flipped to 9.9.9.9, so the version is written into "
        f"the template rather than taken from the pin"
    )
    assert bumped.get("PLUGIN_DIR") == f"{PLUGIN_ROOT}/{PLUGIN_NAME}_9.9.9.9", (
        f"the install marker is {bumped.get('PLUGIN_DIR')!r}, which does not follow the pin — a "
        f"bump would record a version that was never downloaded"
    )


@pytest.mark.parametrize(
    ("what", "constant"),
    [
        ("the checksum itself", "WANT_MD5"),
        ("the version the marker names", "VERSION"),
        ("the plugins directory", "PLUGINS"),
        ("the plugin DLL check", "DLL"),
    ],
)
def test_the_guard_rejects_an_installer_missing_a_pin(what, constant):
    """The red half, by perturbation: each constant removed is a real way this goes wrong."""
    with pytest.raises(AssertionError):
        _assert_install_step(
            without_assignment(script(INSTALLER), constant), load_defaults(JELLYFIN)
        )


def test_the_guard_rejects_an_installer_that_stopped_comparing_its_digest():
    """The red half for the behaviour no constant carries: a pin nothing acts on."""
    installer = script(INSTALLER)
    victim = "got != WANT_MD5"
    assert victim in installer, "the mutation matched nothing — fix the fixture"

    with pytest.raises(AssertionError):
        _assert_install_step(
            installer.replace(victim, "got != got"), load_defaults(JELLYFIN)
        )


def test_every_plugin_installer_is_still_present():
    """Non-vacuity, and the lockstep rule stated where a rename breaks it.

    The role's CLAUDE.md pins `jellyfin_k8s_image` against the targetAbi of every installed
    plugin. That rule is only checkable if the census of installers is known, and each one is
    guarded by its own file — so a new installer added without a guard, or one renamed out
    from under its guard, is exactly what this asserts against.

    Read off the RENDERED Deployment's init containers: a template scan for `- name: install-`
    also matches a container the role declares and then never schedules.

    NOT NAMED FOR A COUNT: a test whose name carries a number lies from the next addition on.
    """
    declared = {name for name in init_containers() if name.startswith("install-")}

    assert declared == set(INSTALLERS), (
        f"jellyfin's plugin installers are {sorted(declared)}. Each one pins the image "
        f"through its targetAbi and each is guarded by its own test file — add or rename one "
        f"and the census in _jellyfin_plugins.INSTALLERS must move with it."
    )


# ── Renovate coverage ───────────────────────────────────────────────────────────────────────
# The pin has no update signal without a dedicated manager: renovate.json's k8s-images
# manager keys on `_image:`, so a plugin version, its URL and its MD5 are invisible to every
# other manager and age silently — which reads exactly like a plugin with no updates available.

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
    major = load_defaults(JELLYFIN)["jellyfin_k8s_webhook_version"].split(".")[0]

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
    major = load_defaults(JELLYFIN)["jellyfin_k8s_webhook_version"].split(".")[0]

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

    # Only a later rule that sets automerge or groupName can override this rule's. A k8s-path rule
    # setting neither (the pinDigests rule, #3281, which must sit last) is no threat to it.
    k8s_plane = [
        i
        for i, r in enumerate(rules)
        if ("automerge" in r or "groupName" in r)
        and any(p.startswith("ansible/roles/k8s/") for p in r.get("matchFileNames", []))
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
