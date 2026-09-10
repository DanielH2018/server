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

Run: uv run pytest ansible/tests/services/test_mergeversions_install.py
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
GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _defaults() -> dict:
    return yaml_fast.safe_load(DEFAULTS.read_text())


def _version_tuple(text: str, what: str) -> tuple[int, ...]:
    match = LEADING_VERSION.match(text)
    assert match, f"{what} does not start with a dotted version: {text!r}"
    return tuple(int(part) for part in match.group(1).split("."))


def _padded(left: tuple[int, ...], right: tuple[int, ...]) -> tuple[tuple, tuple]:
    """Both tuples zero-padded, so a prefix tie does not decide the comparison.

    `(10, 11, 0, 0) <= (10, 11, 11)` compares a four-part targetAbi against a three-part image
    tag; without padding the longer tuple wins ties and the pin that is correct fails.
    """
    width = max(len(left), len(right))
    return left + (0,) * (width - len(left)), right + (0,) * (width - len(right))


def test_the_release_url_carries_the_pinned_version():
    """A bump that misses the URL installs the OLD build under the NEW marker.

    The install marker is the directory name, which carries
    `jellyfin_k8s_mergeversions_version` — so the two drifting apart latches a build that was
    never downloaded, and nothing reconciles it because the installer's own guard is satisfied.
    """
    defaults = _defaults()
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
    defaults = _defaults()
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
    defaults = _defaults()
    target_abi = _version_tuple(
        defaults["jellyfin_k8s_mergeversions_target_abi"],
        "jellyfin_k8s_mergeversions_target_abi",
    )
    image = defaults["jellyfin_k8s_image"]
    server = _version_tuple(image.rsplit(":", 1)[-1], "the jellyfin image tag")

    abi, srv = _padded(target_abi, server)
    assert abi <= srv, (
        f"Merge Versions {defaults['jellyfin_k8s_mergeversions_version']} targets Jellyfin "
        f"{defaults['jellyfin_k8s_mergeversions_target_abi']}, but jellyfin_k8s_image is "
        f"{image}.\nJellyfin's loader rejects a plugin built for a newer server, silently — the "
        f"rollout stays green and the plugin never appears in GET /Plugins. The 12.0.0 release "
        f"targets 12.0; take the newest release on the line the image satisfies."
    )


def _assert_install_step(template: str, version: str) -> None:
    """Everything the rendered deployment must carry for the install to work.

    Factored out so the same assertions can run against a MUTATED template. A guard that is
    only ever handed the real file is a guard nobody has seen fail.
    """
    assert "- name: install-merge-versions" in template, (
        "the deployment no longer declares the install-merge-versions init container"
    )
    assert "{{ jellyfin_k8s_mergeversions_sha256 }}" in template, (
        "the install-merge-versions init container no longer templates "
        "jellyfin_k8s_mergeversions_sha256 — an unpinned download is whatever the release "
        "asset has been replaced with"
    )
    assert "if got != WANT_SHA256:" in template, (
        "the install-merge-versions init container downloads the archive but no longer "
        "COMPARES its digest — the pin is present and inert"
    )
    assert "{{ jellyfin_k8s_mergeversions_version }}" in template, (
        "the install-merge-versions init container no longer templates "
        "jellyfin_k8s_mergeversions_version — the install marker would stop tracking the pin"
    )
    assert version not in template, (
        f"the version {version!r} is written literally into {DEPLOYMENT.name}. Take it from "
        f"jellyfin_k8s_mergeversions_version instead."
    )
    assert 'PLUGINS = Path("/config/data/plugins")' in template, (
        "the install-merge-versions init container no longer targets /config/data/plugins. "
        "Jellyfin scans only that directory — installing anywhere else reports success and "
        "leaves the plugin unloaded, behind a green rollout."
    )
    assert 'PLUGIN_DIR = PLUGINS / ("Merge Versions_" + VERSION)' in template, (
        "the plugin directory no longer follows Jellyfin's <Name>_<Version> layout"
    )
    assert 'DLL = "Jellyfin.Plugin.MergeVersions.dll"' in template, (
        "the install-merge-versions init container no longer checks for the plugin's own DLL. "
        "An archive that extracts without raising but carries no DLL leaves a directory "
        "Jellyfin silently ignores."
    )
    # The half that separates this install from Webhook's: no meta.json in the zip.
    assert '"guid": GUID,' in template and '"targetAbi": TARGET_ABI,' in template, (
        "the install-merge-versions init container no longer writes the plugin's guid and "
        "targetAbi into meta.json. The release ships the DLL alone, so without that file "
        "Jellyfin invents a manifest from the directory name — the id becomes an MD5 of the "
        "folder and AutoUpdate goes false."
    )
    assert '(staged / "meta.json").write_text' in template, (
        "the install-merge-versions init container no longer WRITES meta.json — the fields are "
        "composed and discarded"
    )


def test_the_rendered_deployment_carries_the_pinned_install_step():
    _assert_install_step(
        DEPLOYMENT.read_text(), _defaults()["jellyfin_k8s_mergeversions_version"]
    )


@pytest.mark.parametrize(
    ("what", "victim"),
    [
        ("the checksum comparison", "if got != WANT_SHA256:"),
        ("the checksum itself", "{{ jellyfin_k8s_mergeversions_sha256 }}"),
        ("the whole init container", "- name: install-merge-versions"),
        ("the plugin DLL check", 'DLL = "Jellyfin.Plugin.MergeVersions.dll"'),
        (
            "the plugin directory layout",
            'PLUGIN_DIR = PLUGINS / ("Merge Versions_" + VERSION)',
        ),
        ("the meta.json write", '(staged / "meta.json").write_text'),
        ("the guid in meta.json", '"guid": GUID,'),
    ],
)
def test_the_guard_rejects_a_template_missing_the_step(what, victim):
    """The red half. Each removal above is a real way this install goes quietly wrong."""
    mutated = DEPLOYMENT.read_text().replace(victim, "")
    assert victim not in mutated, (
        f"the mutation for {what} matched nothing — fix the fixture"
    )

    with pytest.raises(AssertionError):
        _assert_install_step(mutated, _defaults()["jellyfin_k8s_mergeversions_version"])


# ── Renovate coverage (#1616, the finding #1557 exists for) ──────────────────────────────────
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
    version = _defaults()["jellyfin_k8s_mergeversions_version"]

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
    version = _defaults()["jellyfin_k8s_mergeversions_version"]

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
        f"where it stands. Append it at the end of packageRules, beside its three siblings."
    )
