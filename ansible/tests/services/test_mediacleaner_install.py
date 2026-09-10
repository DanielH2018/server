#!/usr/bin/env python3
"""The Media Cleaner plugin install step must exist, be checksum-pinned, and be loadable.

Fifth of jellyfin's plugin installers, and the one whose blast radius justifies the pin: Media
Cleaner DELETES MEDIA FILES on a schedule, by rule. It ran unmanaged on the `jellyfin-config`
PVC from a dashboard install until #1619 — no pinned version, no checksum, no recorded
`targetAbi`, no test — so an image bump could drop it silently, Jellyfin's loader refusing a
plugin built for a newer server without logging anything.

This install has a hazard none of the four siblings has. Upstream ships ONE release tag
(`v3.2.0`) carrying THREE per-ABI assets, and the published manifest publishes each as its own
version — `3.2.0.101007`, `3.2.0.101100`, `3.2.0.101109` — where the fourth segment is
`<major><minor:02d><patch:02d>` of the `targetAbi`. All three are the same plugin release, so
picking the wrong one is invisible: the URL looks right, the version looks right, and the plugin
is built against a server the image is not. `test_the_version_suffix_decodes_to_the_target_abi`
re-derives that encoding rather than trusting three hand-copied values to agree.

Unlike Intro Skipper and Merge Versions, the zip carries its own `meta.json`, so nothing writes
one — this is Webhook's shape.

Run: uv run pytest ansible/tests/services/test_mediacleaner_install.py
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
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _defaults() -> dict:
    return yaml_fast.safe_load(DEFAULTS.read_text())


def _version_tuple(text: str, what: str) -> tuple[int, ...]:
    match = LEADING_VERSION.match(text)
    assert match, f"{what} does not start with a dotted version: {text!r}"
    return tuple(int(part) for part in match.group(1).split("."))


def _padded(left: tuple[int, ...], right: tuple[int, ...]) -> tuple[tuple, tuple]:
    """Both tuples zero-padded, so a prefix tie does not decide the comparison.

    `(10, 11, 9, 0) <= (10, 11, 11)` compares a four-part targetAbi against a three-part image
    tag; without padding the longer tuple wins ties and the pin that is correct fails.
    """
    width = max(len(left), len(right))
    return left + (0,) * (width - len(left)), right + (0,) * (width - len(right))


def _assert_release_tag(defaults: dict) -> None:
    """The URL's release tag must name the version's first three segments.

    Factored out alongside `_assert_pin_agrees` so the mutation test runs this logic rather than
    a second copy of it.
    """
    version = defaults["jellyfin_k8s_mediacleaner_version"]
    url = defaults["jellyfin_k8s_mediacleaner_url"]
    release = ".".join(version.split(".")[:3])

    assert f"/download/v{release}/" in url, (
        f"jellyfin_k8s_mediacleaner_version is {version!r}, whose plugin release is "
        f"{release!r}, but jellyfin_k8s_mediacleaner_url does not carry it as the release tag: "
        f"{url}"
    )


def test_the_release_tag_in_the_url_carries_the_plugin_release():
    """A bump that misses the URL installs the OLD build under the NEW marker.

    The install marker is the directory name, which carries
    `jellyfin_k8s_mediacleaner_version` — so the two drifting apart latches a build that was
    never downloaded, and nothing reconciles it because the installer's own guard is satisfied.

    Only the first three segments appear in the tag: the fourth is the ABI suffix, which upstream
    never puts in a tag. The next test covers that half.
    """
    _assert_release_tag(_defaults())


def _assert_pin_agrees(defaults: dict) -> None:
    """The three ABI-bearing values must name one build.

    Factored out so the MUTATION test below runs this exact logic rather than its own copy of the
    formula. A mutation test carrying a second implementation passes while the real check is
    broken — it proves the copy rejects the input, not the guard.
    """
    version = defaults["jellyfin_k8s_mediacleaner_version"]
    target_abi = defaults["jellyfin_k8s_mediacleaner_target_abi"]
    url = defaults["jellyfin_k8s_mediacleaner_url"]

    parts = version.split(".")
    assert len(parts) == 4, (
        f"jellyfin_k8s_mediacleaner_version is {version!r}; upstream's manifest versions are "
        f"four-part, with the fourth segment encoding the targetAbi. A three-part version means "
        f"the pin was taken from the release TAG rather than from the published manifest."
    )

    abi = _version_tuple(target_abi, "jellyfin_k8s_mediacleaner_target_abi")
    assert len(abi) >= 3, f"targetAbi {target_abi!r} has fewer than three segments"
    expected_suffix = f"{abi[0]}{abi[1]:02d}{abi[2]:02d}"

    assert parts[3] == expected_suffix, (
        f"jellyfin_k8s_mediacleaner_version ends in {parts[3]!r}, but "
        f"jellyfin_k8s_mediacleaner_target_abi {target_abi!r} encodes to {expected_suffix!r}.\n"
        f"Upstream publishes one release as several versions differing only in this segment — "
        f"3.2.0.101007 / 3.2.0.101100 / 3.2.0.101109 for ABIs 10.10.7 / 10.11.0 / 10.11.9. The "
        f"two disagreeing means the version and the targetAbi name different builds."
    )

    # And the asset filename is the third place the same ABI is written.
    asset_abi = f"{abi[0]}.{abi[1]}.{abi[2]}"
    assert f"MediaCleaner-{asset_abi}.zip" in url, (
        f"jellyfin_k8s_mediacleaner_target_abi is {target_abi!r}, so the asset should be "
        f"MediaCleaner-{asset_abi}.zip — but the URL names a different one: {url}\n"
        f"The three per-ABI assets on one release tag are indistinguishable once downloaded, so "
        f"the wrong one installs cleanly and never loads."
    )


def test_the_version_suffix_decodes_to_the_target_abi():
    """The guard this plugin exists to need, and the one no sibling pin has.

    One upstream release tag ships three per-ABI assets, published as three manifest versions
    differing only in a fourth segment that encodes the targetAbi as
    `<major><minor:02d><patch:02d>`. So three values have to agree — the version suffix, the
    targetAbi var, and the ABI in the asset filename — and all three are hand-copied. A
    disagreement is SILENT: the URL resolves, the install succeeds, and Jellyfin refuses to load
    a plugin built for a server the image is not, without logging a failure.

    Derived rather than compared against a literal, so the check survives the next release.
    """
    _assert_pin_agrees(_defaults())


def test_the_digest_is_the_right_shape():
    """A truncated sha256 is a pin that looks present and compares against nothing useful."""
    sha = _defaults()["jellyfin_k8s_mediacleaner_sha256"]
    assert SHA256.match(sha), (
        f"jellyfin_k8s_mediacleaner_sha256 is not 64 lowercase hex characters: {sha!r}"
    )


def test_the_plugin_target_abi_does_not_exceed_the_server():
    """The constraint the pin exists to satisfy, checked rather than remembered."""
    defaults = _defaults()
    target_abi = _version_tuple(
        defaults["jellyfin_k8s_mediacleaner_target_abi"],
        "jellyfin_k8s_mediacleaner_target_abi",
    )
    image = defaults["jellyfin_k8s_image"]
    server = _version_tuple(image.rsplit(":", 1)[-1], "the jellyfin image tag")

    abi, srv = _padded(target_abi, server)
    assert abi <= srv, (
        f"Media Cleaner {defaults['jellyfin_k8s_mediacleaner_version']} targets Jellyfin "
        f"{defaults['jellyfin_k8s_mediacleaner_target_abi']}, but jellyfin_k8s_image is "
        f"{image}.\nJellyfin's loader rejects a plugin built for a newer server, silently — the "
        f"rollout stays green and the plugin never appears in GET /Plugins. For a plugin that "
        f"DELETES MEDIA, silently not running is the better of the two failures, but it is still "
        f"a fleet that thinks a cleaner is armed and has none."
    )


def _assert_install_step(template: str, version: str) -> None:
    """Everything the rendered deployment must carry for the install to work.

    Factored out so the same assertions can run against a MUTATED template. A guard that is
    only ever handed the real file is a guard nobody has seen fail.
    """
    assert "- name: install-media-cleaner" in template, (
        "the deployment no longer declares the install-media-cleaner init container"
    )
    assert "{{ jellyfin_k8s_mediacleaner_sha256 }}" in template, (
        "the install-media-cleaner init container no longer templates "
        "jellyfin_k8s_mediacleaner_sha256 — an unpinned download of a plugin that deletes media "
        "is whatever the release asset has been replaced with"
    )
    assert "if got != WANT_SHA256:" in template, (
        "the install-media-cleaner init container downloads the archive but no longer COMPARES "
        "its digest — the pin is present and inert"
    )
    assert "{{ jellyfin_k8s_mediacleaner_version }}" in template, (
        "the install-media-cleaner init container no longer templates "
        "jellyfin_k8s_mediacleaner_version — the install marker would stop tracking the pin"
    )
    assert version not in template, (
        f"the version {version!r} is written literally into {DEPLOYMENT.name}. Take it from "
        f"jellyfin_k8s_mediacleaner_version instead."
    )
    assert 'PLUGINS = Path("/config/data/plugins")' in template, (
        "the install-media-cleaner init container no longer targets /config/data/plugins. "
        "Jellyfin scans only that directory — installing anywhere else reports success and "
        "leaves the plugin unloaded, behind a green rollout."
    )
    assert 'PLUGIN_DIR = PLUGINS / ("Media Cleaner_" + VERSION)' in template, (
        "the plugin directory no longer follows Jellyfin's <Name>_<Version> layout. The name "
        "also has to match what the dashboard installer wrote, or the guard misses the copy "
        "already on the volume and reinstalls on every pod start."
    )
    assert 'DLL = "MediaCleaner.dll"' in template, (
        "the install-media-cleaner init container no longer checks for the plugin's own DLL"
    )
    assert 'DEP_DLL = "MediaCleaner.Core.dll"' in template, (
        "the install-media-cleaner init container no longer checks for MediaCleaner.Core.dll. "
        "The plugin does not load without it, so an archive whose layout changed would install "
        "cleanly and load nothing."
    )
    assert "for needed in (DLL, DEP_DLL):" in template, (
        "the install-media-cleaner init container names both DLLs but no longer LOOPS over "
        "them — one of the two checks would be dead"
    )


def test_the_rendered_deployment_carries_the_pinned_install_step():
    _assert_install_step(
        DEPLOYMENT.read_text(), _defaults()["jellyfin_k8s_mediacleaner_version"]
    )


@pytest.mark.parametrize(
    ("what", "victim"),
    [
        ("the checksum comparison", "if got != WANT_SHA256:"),
        ("the checksum itself", "{{ jellyfin_k8s_mediacleaner_sha256 }}"),
        ("the whole init container", "- name: install-media-cleaner"),
        ("the plugin DLL check", 'DLL = "MediaCleaner.dll"'),
        ("the dependency DLL check", 'DEP_DLL = "MediaCleaner.Core.dll"'),
        ("the loop over both DLLs", "for needed in (DLL, DEP_DLL):"),
        (
            "the plugin directory layout",
            'PLUGIN_DIR = PLUGINS / ("Media Cleaner_" + VERSION)',
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
        _assert_install_step(mutated, _defaults()["jellyfin_k8s_mediacleaner_version"])


@pytest.mark.parametrize(
    ("what", "before", "after"),
    [
        ("a version suffix naming another ABI", "3.2.0.101109", "3.2.0.101100"),
        ("a targetAbi naming another asset", "10.11.9.0", "10.11.0.0"),
        (
            "a release tag the version does not name",
            "/download/v3.2.0/",
            "/download/v3.1.0/",
        ),
    ],
)
def test_the_pin_guards_reject_a_mismatched_defaults_file(what, before, after):
    """The red half for the three-way ABI agreement, which is this pin's whole hazard.

    Each mutation is a value a careless bump really produces, and each leaves a defaults file
    that installs cleanly and loads nothing.

    It calls the REAL helpers — `_assert_pin_agrees` and `_assert_release_tag` — rather than
    re-deriving the encoding. A mutation test with its own copy of the formula passes while the
    formula under test is broken, because what it proves is that the copy rejects the input.
    """
    text = DEFAULTS.read_text()
    assert text.count(before) >= 1, (
        f"the mutation for {what} matched nothing — fix the fixture"
    )
    mutated = yaml_fast.safe_load(text.replace(before, after))

    with pytest.raises(AssertionError):
        _assert_release_tag(mutated)
        _assert_pin_agrees(mutated)


# ── Renovate coverage (the finding #1557 exists for) ─────────────────────────────────────────
# renovate.json's k8s-images manager keys on `_image:`, so a plugin version, its URL and its
# checksum are invisible to every manager without a dedicated one — and a pin with no update
# signal reads exactly like a plugin with no updates available.

DEP = "shemanaev/jellyfin-plugin-media-cleaner"


def _renovate() -> dict:
    return json.loads(RENOVATE.read_text())


def _manager() -> dict:
    manager = next(
        (m for m in _renovate()["customManagers"] if m.get("depNameTemplate") == DEP),
        None,
    )
    assert manager, (
        f"renovate.json no longer carries a customManager for {DEP} — the Media Cleaner pin "
        f"ages with no update signal at all, which is the state #1619 was filed against"
    )
    return manager


def test_every_manager_pattern_still_matches_the_pinned_release():
    """A manager whose matchStrings match nothing is inert, and inert looks like up-to-date.

    Both patterns must capture the SAME three-part release, because Renovate rewrites the URL's
    tag and the version var in step — a rewrite that reached only one would leave the marker
    naming a build that was never downloaded.
    """
    text = DEFAULTS.read_text()
    release = ".".join(_defaults()["jellyfin_k8s_mediacleaner_version"].split(".")[:3])

    for pattern in _manager()["matchStrings"]:
        found = re.findall(pattern.replace("(?<", "(?P<"), text)
        assert found, (
            f"the Media Cleaner Renovate manager's matchString {pattern!r} matches nothing in "
            f"{DEFAULTS.name} — the manager is inert, which reads as 'no updates available'"
        )
        assert set(found) == {release}, (
            f"the Media Cleaner Renovate manager's matchString {pattern!r} captured "
            f"{sorted(set(found))} in {DEFAULTS.name}, not the pinned release {release!r}"
        )


def test_the_manager_admits_the_pinned_tag():
    """A version template that cannot parse the pin offers nothing, including the pin itself."""
    manager = _manager()
    anchor = re.compile(manager["extractVersionTemplate"].replace("(?<", "(?P<"))
    release = ".".join(_defaults()["jellyfin_k8s_mediacleaner_version"].split(".")[:3])

    assert anchor.match(f"v{release}"), (
        f"the Media Cleaner Renovate manager's extractVersionTemplate {anchor.pattern!r} does "
        f"not admit the pinned tag v{release} — the manager offers nothing at all"
    )
    # Upstream's older tags are four-part (`v3.1.0.0`); the versioning has to order both shapes
    # or the newest release is unreachable from the oldest pin.
    versioning = re.compile(
        manager["versioningTemplate"].removeprefix("regex:").replace("(?<", "(?P<")
    )
    assert versioning.match(release) and versioning.match("3.1.0.0"), (
        f"the Media Cleaner Renovate manager's versioningTemplate "
        f"{manager['versioningTemplate']!r} does not parse both of upstream's tag shapes "
        f"(three-part v3.2.0 and four-part v3.1.0.0) — one of them becomes unorderable and the "
        f"manager silently skips it"
    )


def test_the_bump_is_never_automerged():
    """The finish is manual, and for this plugin that is the point.

    A bump has to pick the right per-ABI asset out of several on one tag, re-verify the digest,
    and carry the targetAbi across — none of which a regex manager can do. An automerged bump
    would arm a different build of a media-deleting plugin with nothing checked.

    The rule's POSITION is load-bearing, exactly as the four sibling rules' own descriptions
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
        f"where it stands. Append it at the end of packageRules, beside its four siblings."
    )
