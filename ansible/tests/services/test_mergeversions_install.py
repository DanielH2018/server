#!/usr/bin/env python3
"""The Merge Versions plugin install step must exist, be checksum-pinned, and be loadable.

Fourth of jellyfin's plugin installers, and it repeats the hazard the other three record:
Jellyfin's loader refuses a plugin whose `targetAbi` is newer than the running server and says
nothing about it — the directory sits on disk, `GET /Plugins` omits it, the rollout is green.

The trap has intro-skipper's shape here. Upstream tags the release LINE by Jellyfin version —
`10.11.0.1` and `12.0.0` are the two live lines — so "the newest release" is routinely the wrong
one: 12.0.0 declares targetAbi 12.0, which a 10.11 server rejects.

Unlike Webhook, this release ships the DLL alone with no `meta.json`, so the init container
writes one from the guid, name and targetAbi. A missing manifest is not a failure — Jellyfin
invents one from the directory name, setting the id to an MD5 of the folder and AutoUpdate to
false — which is why the write is asserted here rather than left to reading the template.

The install step is read out of the RENDERED Deployment (`_jellyfin_plugins`), so each assertion
is about a value the pod receives rather than a line of `deployment.yaml.j2`. The pin reaching the
container is proved by rendering the role again with the version flipped, which is the half a
single render cannot show: a literal and a templated expression render identically.

Run: uv run pytest ansible/tests/services/test_mergeversions_install.py
"""

import json
import re

import pytest

from _helpers import ANSIBLE, REPO, load_defaults
from _jellyfin_plugins import (
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

INSTALLER = "install-merge-versions"
PLUGIN_NAME = "Merge Versions"

GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def test_the_release_url_carries_the_pinned_version():
    """A bump that misses the URL installs the OLD build under the NEW marker.

    The install marker is the directory name, which carries
    `jellyfin_k8s_mergeversions_version` — so the two drifting apart latches a build that was
    never downloaded, and nothing reconciles it because the installer's own guard is satisfied.
    """
    defaults = load_defaults(JELLYFIN)
    version = defaults["jellyfin_k8s_mergeversions_version"]
    url = defaults["jellyfin_k8s_mergeversions_url"]

    assert f"/download/{version}/" in url, (
        f"jellyfin_k8s_mergeversions_version is {version!r} but "
        f"jellyfin_k8s_mergeversions_url does not carry it as the release tag: {url}"
    )


def test_the_digest_and_guid_are_the_right_shape():
    """A truncated sha256 or a mistyped guid is a pin that looks present and is not.

    The digest is compared as a string by the init container, so a wrong LENGTH fails the
    install loudly — but a guid of the wrong shape does not fail anything: it is written
    straight into meta.json, and Jellyfin binds the dashboard entry and the plugin's
    configuration directory to whatever is there.
    """
    defaults = load_defaults(JELLYFIN)
    sha = defaults["jellyfin_k8s_mergeversions_sha256"]
    guid = defaults["jellyfin_k8s_mergeversions_guid"]

    assert SHA256.match(sha), (
        f"jellyfin_k8s_mergeversions_sha256 is not 64 lowercase hex characters: {sha!r}"
    )
    assert GUID.match(guid), (
        f"jellyfin_k8s_mergeversions_guid is not a dashed lowercase UUID: {guid!r}. It is "
        f"written verbatim into the meta.json the init container composes, and Jellyfin binds "
        f"the dashboard entry and the plugin's config directory to it."
    )


def test_the_plugin_target_abi_does_not_exceed_the_server():
    """The constraint the pin exists to satisfy, checked rather than remembered."""
    defaults = load_defaults(JELLYFIN)
    target_abi = version_tuple(
        defaults["jellyfin_k8s_mergeversions_target_abi"],
        "jellyfin_k8s_mergeversions_target_abi",
    )
    image = defaults["jellyfin_k8s_image"]
    server = version_tuple(image.rsplit(":", 1)[-1], "the jellyfin image tag")

    abi, srv = padded(target_abi, server)
    assert abi <= srv, (
        f"Merge Versions {defaults['jellyfin_k8s_mergeversions_version']} targets Jellyfin "
        f"{defaults['jellyfin_k8s_mergeversions_target_abi']}, but jellyfin_k8s_image is "
        f"{image}.\nJellyfin's loader rejects a plugin built for a newer server, silently — the "
        f"rollout stays green and the plugin never appears in GET /Plugins. The 12.0.0 release "
        f"targets 12.0; take the newest release on the line the image satisfies."
    )


def _assert_install_step(installer: str, defaults: dict) -> None:
    """Everything the installer the pod runs must carry for the install to work.

    Factored out so the same assertions can run against a MUTATED script. A guard that is only
    ever handed the real one is a guard nobody has seen fail.

    Args:
        installer: the Python the `install-merge-versions` init container runs.
        defaults: the jellyfin role's defaults, holding the pins the script must agree with.
    """
    consts = plugin_constants(installer)
    version = defaults["jellyfin_k8s_mergeversions_version"]

    assert consts.get("WANT_SHA256") == defaults["jellyfin_k8s_mergeversions_sha256"], (
        f"the installer checks sha256 {consts.get('WANT_SHA256')!r}, but the role pins "
        f"{defaults['jellyfin_k8s_mergeversions_sha256']!r} — an unpinned download is whatever "
        f"the release asset has been replaced with"
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
    assert consts.get("DLL") == "Jellyfin.Plugin.MergeVersions.dll", (
        f"the installer checks for {consts.get('DLL')!r}, not the plugin's own DLL. An archive "
        f"that extracts without raising but carries no DLL leaves a directory Jellyfin "
        f"silently ignores."
    )
    # The half that separates this install from Webhook's: no meta.json in the zip.
    assert consts.get("GUID") == defaults["jellyfin_k8s_mergeversions_guid"], (
        f"the manifest the installer writes carries guid {consts.get('GUID')!r} while the role "
        f"pins {defaults['jellyfin_k8s_mergeversions_guid']!r}. Jellyfin keys the plugin by it, "
        f"so a wrong one reads as a different plugin on every restart."
    )
    assert (
        consts.get("TARGET_ABI") == defaults["jellyfin_k8s_mergeversions_target_abi"]
    ), (
        f"the manifest declares targetAbi {consts.get('TARGET_ABI')!r} while the role pins "
        f"{defaults['jellyfin_k8s_mergeversions_target_abi']!r} — the value the ABI guard above "
        f"compares against the image"
    )
    assert '(staged / "meta.json").write_text' in installer, (
        "the installer no longer WRITES meta.json — the fields are composed and discarded. The "
        "release ships the DLL alone, so without that file Jellyfin invents a manifest from the "
        "directory name: the id becomes an MD5 of the folder and AutoUpdate goes false."
    )


def test_the_deployment_declares_the_installer():
    """Non-vacuity: every assertion below reads one init container out of the render."""
    containers = init_containers()
    assert INSTALLER in containers, (
        f"the rendered jellyfin Deployment no longer declares {INSTALLER}, so Merge Versions is "
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
        script(INSTALLER, {"jellyfin_k8s_mergeversions_version": "9.9.9.9"})
    )
    assert bumped.get("VERSION") == "9.9.9.9", (
        f"the installer still installs {bumped.get('VERSION')!r} after "
        f"jellyfin_k8s_mergeversions_version was flipped to 9.9.9.9, so the version is written "
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
        ("the plugin DLL check", "DLL"),
        ("the guid in meta.json", "GUID"),
        ("the targetAbi in meta.json", "TARGET_ABI"),
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

    Both are perturbations of the SCRIPT the pod runs, not of `deployment.yaml.j2`: a pin can be
    present and never acted on, which no constant assertion can see.
    """
    installer = script(INSTALLER)
    assert victim in installer, (
        f"the mutation for {what} matched nothing — fix the fixture"
    )

    with pytest.raises(AssertionError):
        _assert_install_step(
            installer.replace(victim, replacement), load_defaults(JELLYFIN)
        )


# ── Renovate coverage ────────────────────────────────────────────────────────────────────────
# renovate.json's k8s-images manager keys on `_image:`, so a plugin version, its URL and its
# checksum are invisible to every manager without a dedicated one — and a pin with no update
# signal reads exactly like a plugin with no updates available.

DEP = "danieladov/jellyfin-plugin-mergeversions"


def _renovate() -> dict:
    return json.loads(RENOVATE.read_text())


def _manager() -> dict:
    manager = next(
        (m for m in _renovate()["customManagers"] if m.get("depNameTemplate") == DEP),
        None,
    )
    assert manager, (
        f"renovate.json no longer carries a customManager for {DEP} — the Merge Versions pin "
        f"ages with no update signal at all"
    )
    return manager


def test_every_manager_pattern_still_matches_the_pinned_version():
    """A manager whose matchStrings match nothing is inert, and inert looks like up-to-date.

    Both patterns must capture the SAME version, because Renovate rewrites the URL and the
    version var in step — a rewrite that reached only one would leave the marker naming a build
    that was never downloaded.
    """
    text = DEFAULTS.read_text()
    version = load_defaults(JELLYFIN)["jellyfin_k8s_mergeversions_version"]

    for pattern in _manager()["matchStrings"]:
        found = re.findall(pattern.replace("(?<", "(?P<"), text)
        assert found, (
            f"the Merge Versions Renovate manager's matchString {pattern!r} matches nothing "
            f"in {DEFAULTS.name} — the manager is inert, which reads as 'no updates available'"
        )
        assert set(found) == {version}, (
            f"the Merge Versions Renovate manager's matchString {pattern!r} captured "
            f"{sorted(set(found))} in {DEFAULTS.name}, not the pinned {version!r}"
        )


def test_the_manager_anchor_admits_the_pin_and_drops_the_jellyfin_12_line():
    """The anchor is what keeps 'the newest release' off a 10.11 server.

    Upstream tags the release LINE by Jellyfin version, so the datasource's newest tag is
    `12.0.0` — a build declaring targetAbi 12.0, which the deployed 10.11 server's loader
    rejects without logging anything.
    """
    anchor = re.compile(_manager()["extractVersionTemplate"].replace("(?<", "(?P<"))
    version = load_defaults(JELLYFIN)["jellyfin_k8s_mergeversions_version"]

    assert anchor.match(version), (
        f"the Merge Versions Renovate manager's extractVersionTemplate {anchor.pattern!r} "
        f"does not admit the pinned tag {version} — the manager offers nothing, including the "
        f"release it is pinned to"
    )
    assert not anchor.match("12.0.0"), (
        f"the Merge Versions Renovate manager's extractVersionTemplate {anchor.pattern!r} "
        f"admits 12.0.0, which declares targetAbi 12.0 and which the deployed 10.11 server "
        f"rejects without logging anything. Raise this anchor only with jellyfin_k8s_image."
    )


def test_the_bump_is_never_automerged():
    """The finish is manual: the sha256, the targetAbi and the guid all come from upstream.

    The rule's POSITION is load-bearing, exactly as the three sibling rules' own descriptions
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
        f"where it stands. Append it at the end of packageRules, beside its three siblings."
    )
