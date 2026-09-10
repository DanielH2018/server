#!/usr/bin/env python3
"""The Trakt plugin install step must exist, be checksum-pinned, and be loadable.

Sixth of jellyfin's plugin installers (#1617). Trakt scrobbles playback and syncs watched
history to a trakt.tv account — the non-anime counterpart to `jellyfin-ani-sync`.

Two things make this pin easy to get wrong, and both are silent:

- **The release LINE.** Upstream's newest release targets Jellyfin 12 (`31.0.0.0` declares
  `targetAbi` 12.0.0.0), and Jellyfin's loader refuses a plugin built for a newer server without
  logging a failure — the directory sits on disk, `GET /Plugins` omits it, the rollout stays
  green. So *the newest release is the wrong one* while the image is a 10.11 build, the same trap
  the Intro Skipper, Webhook and Merge Versions pins record.
- **The Renovate anchor.** The manager exists so the pin does not age with no signal (#1557), and
  its `extractVersionTemplate` is what keeps it from offering the Jellyfin 12 line. An anchor
  that admitted 31 would turn the update signal into a silent breakage, so this file asserts the
  anchor both ADMITS the pin and REJECTS the next line — the sibling files only assert the first
  half.

Webhook's shape for the install itself: the zip carries its own `meta.json`, so nothing writes
one and no guid is an input.

Run: uv run pytest ansible/tests/services/test_trakt_install.py
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
    """Both tuples zero-padded, so a prefix tie does not decide the comparison."""
    width = max(len(left), len(right))
    return left + (0,) * (width - len(left)), right + (0,) * (width - len(right))


def _assert_url_carries_the_version(defaults: dict) -> None:
    """The download URL and the version var must name one build.

    Factored out so the mutation test runs this logic rather than a second copy of it.
    """
    version = defaults["jellyfin_k8s_trakt_version"]
    url = defaults["jellyfin_k8s_trakt_url"]
    major = version.split(".")[0]

    assert f"/trakt_{version}.zip" in url, (
        f"jellyfin_k8s_trakt_version is {version!r}, but jellyfin_k8s_trakt_url does not name "
        f"trakt_{version}.zip: {url}\nThe install marker is the directory name, which carries "
        f"the version var — so the two drifting apart latches a build that was never downloaded, "
        f"and the installer's own guard is satisfied by it."
    )
    assert version == f"{major}.0.0.0", (
        f"jellyfin_k8s_trakt_version is {version!r}. Upstream publishes this plugin as "
        f"`<major>.0.0.0` against a bare `v<major>` tag, and the Renovate manager's matchStrings "
        f"capture only that major — a version of another shape leaves the manager inert."
    )


def test_the_url_names_the_pinned_version():
    """A bump that misses the URL installs the OLD build under the NEW marker."""
    _assert_url_carries_the_version(_defaults())


def test_the_digest_is_the_right_shape():
    """A truncated sha256 is a pin that looks present and compares against nothing useful."""
    sha = _defaults()["jellyfin_k8s_trakt_sha256"]
    assert SHA256.match(sha), (
        f"jellyfin_k8s_trakt_sha256 is not 64 lowercase hex characters: {sha!r}"
    )


def test_the_plugin_target_abi_does_not_exceed_the_server():
    """The constraint the pin exists to satisfy, checked rather than remembered."""
    defaults = _defaults()
    target_abi = _version_tuple(
        defaults["jellyfin_k8s_trakt_target_abi"], "jellyfin_k8s_trakt_target_abi"
    )
    image = defaults["jellyfin_k8s_image"]
    server = _version_tuple(image.rsplit(":", 1)[-1], "the jellyfin image tag")

    abi, srv = _padded(target_abi, server)
    assert abi <= srv, (
        f"Trakt {defaults['jellyfin_k8s_trakt_version']} targets Jellyfin "
        f"{defaults['jellyfin_k8s_trakt_target_abi']}, but jellyfin_k8s_image is {image}.\n"
        f"Jellyfin's loader rejects a plugin built for a newer server, silently — the rollout "
        f"stays green and the plugin never appears in GET /Plugins."
    )


def _assert_install_step(template: str, version: str) -> None:
    """Everything the rendered deployment must carry for the install to work.

    Factored out so the same assertions can run against a MUTATED template. A guard that is only
    ever handed the real file is a guard nobody has seen fail.
    """
    assert "- name: install-trakt" in template, (
        "the deployment no longer declares the install-trakt init container"
    )
    assert "{{ jellyfin_k8s_trakt_sha256 }}" in template, (
        "the install-trakt init container no longer templates jellyfin_k8s_trakt_sha256 — an "
        "unpinned download is whatever the release asset has been replaced with"
    )
    assert "if got != WANT_SHA256:" in template, (
        "the install-trakt init container downloads the archive but no longer COMPARES its "
        "digest — the pin is present and inert"
    )
    assert "{{ jellyfin_k8s_trakt_version }}" in template, (
        "the install-trakt init container no longer templates jellyfin_k8s_trakt_version — the "
        "install marker would stop tracking the pin"
    )
    assert version not in template, (
        f"the version {version!r} is written literally into {DEPLOYMENT.name}. Take it from "
        f"jellyfin_k8s_trakt_version instead."
    )
    assert 'PLUGINS = Path("/config/data/plugins")' in template, (
        "the install-trakt init container no longer targets /config/data/plugins. Jellyfin "
        "scans only that directory — installing anywhere else reports success and leaves the "
        "plugin unloaded, behind a green rollout."
    )
    assert 'PLUGIN_DIR = PLUGINS / ("Trakt_" + VERSION)' in template, (
        "the plugin directory no longer follows Jellyfin's <Name>_<Version> layout, with the "
        "name the zip's own meta.json declares. A different name is a second copy of one plugin "
        "the moment anyone installs it from the dashboard as well."
    )
    assert 'DLL = "Trakt.dll"' in template, (
        "the install-trakt init container no longer checks for the plugin's own DLL. An archive "
        "whose layout changed would install cleanly and load nothing."
    )


def test_the_rendered_deployment_carries_the_pinned_install_step():
    _assert_install_step(
        DEPLOYMENT.read_text(), _defaults()["jellyfin_k8s_trakt_version"]
    )


@pytest.mark.parametrize(
    ("what", "victim"),
    [
        ("the checksum comparison", "if got != WANT_SHA256:"),
        ("the checksum itself", "{{ jellyfin_k8s_trakt_sha256 }}"),
        ("the whole init container", "- name: install-trakt"),
        ("the plugin DLL check", 'DLL = "Trakt.dll"'),
        ("the plugin directory layout", 'PLUGIN_DIR = PLUGINS / ("Trakt_" + VERSION)'),
    ],
)
def test_the_guard_rejects_a_template_missing_the_step(what, victim):
    """The red half. Each removal above is a real way this install goes quietly wrong."""
    mutated = DEPLOYMENT.read_text().replace(victim, "")
    assert victim not in mutated, (
        f"the mutation for {what} matched nothing — fix the fixture"
    )

    with pytest.raises(AssertionError):
        _assert_install_step(mutated, _defaults()["jellyfin_k8s_trakt_version"])


@pytest.mark.parametrize(
    ("what", "before", "after"),
    [
        ("a URL naming another release", "trakt_30.0.0.0.zip", "trakt_31.0.0.0.zip"),
        (
            "a version of the wrong shape",
            'jellyfin_k8s_trakt_version: "30.0.0.0"',
            'jellyfin_k8s_trakt_version: "30.1"',
        ),
    ],
)
def test_the_pin_guard_rejects_a_mismatched_defaults_file(what, before, after):
    """The red half for the pin itself, run against a mutated COPY of the real defaults."""
    text = DEFAULTS.read_text()
    assert before in text, f"the mutation for {what} matched nothing — fix the fixture"

    with pytest.raises(AssertionError):
        _assert_url_carries_the_version(
            yaml_fast.safe_load(text.replace(before, after, 1))
        )


# ── Renovate coverage (#1557) ───────────────────────────────────────────────────────────────
# renovate.json's k8s-images manager keys on `_image:`, so a plugin version, its URL and its
# checksum are invisible to every manager without a dedicated one — and a pin with no update
# signal reads exactly like a plugin with no updates available.

DEP = "jellyfin/jellyfin-plugin-trakt"


def _renovate() -> dict:
    return json.loads(RENOVATE.read_text())


def _manager() -> dict:
    manager = next(
        (m for m in _renovate()["customManagers"] if m.get("depNameTemplate") == DEP),
        None,
    )
    assert manager, (
        f"renovate.json no longer carries a customManager for {DEP} — the Trakt pin ages with "
        f"no update signal at all, the state #1557 was filed against"
    )
    return manager


def _anchor() -> re.Pattern:
    return re.compile(_manager()["extractVersionTemplate"].replace("(?<", "(?P<"))


def test_every_manager_pattern_still_matches_the_pinned_release():
    """A manager whose matchStrings match nothing is inert, and inert looks like up-to-date.

    Both patterns must capture the SAME major, because Renovate rewrites the URL's asset name
    and the version var in step — a rewrite that reached only one would leave the marker naming
    a build that was never downloaded.
    """
    text = DEFAULTS.read_text()
    major = _defaults()["jellyfin_k8s_trakt_version"].split(".")[0]

    for pattern in _manager()["matchStrings"]:
        found = re.findall(pattern.replace("(?<", "(?P<"), text)
        assert found, (
            f"the Trakt Renovate manager's matchString {pattern!r} matches nothing in "
            f"{DEFAULTS.name} — the manager is inert, which reads as 'no updates available'"
        )
        assert set(found) == {major}, (
            f"the Trakt Renovate manager's matchString {pattern!r} captured {sorted(set(found))} "
            f"in {DEFAULTS.name}, not the pinned major {major!r}"
        )


def test_the_anchor_admits_the_pinned_tag():
    """A version template that cannot parse the pin offers nothing, including the pin itself."""
    major = _defaults()["jellyfin_k8s_trakt_version"].split(".")[0]
    anchor = _anchor()

    assert anchor.match(f"v{major}"), (
        f"the Trakt Renovate manager's extractVersionTemplate {anchor.pattern!r} does not admit "
        f"the pinned tag v{major} — the manager offers nothing at all"
    )
    versioning = re.compile(
        _manager()["versioningTemplate"].removeprefix("regex:").replace("(?<", "(?P<")
    )
    assert versioning.match(major), (
        f"the Trakt Renovate manager's versioningTemplate "
        f"{_manager()['versioningTemplate']!r} does not parse the bare major upstream tags "
        f"(v{major}) — the offered version is unorderable and the manager silently skips it"
    )


def test_the_anchor_rejects_the_jellyfin_12_line():
    """The half the anchor exists for, and the half no sibling file asserts.

    Trakt 31.0.0.0 declares targetAbi 12.0.0.0 and 32 is on the same line. An anchor that
    admitted either would offer a plugin this server's loader refuses WITHOUT LOGGING — so the
    Renovate signal the manager was added to provide would become the breakage instead. Raise
    the anchor only when jellyfin_k8s_image moves to Jellyfin 12.
    """
    anchor = _anchor()
    for rejected in ("v31", "v32"):
        assert not anchor.match(rejected), (
            f"the Trakt Renovate manager's extractVersionTemplate {anchor.pattern!r} admits "
            f"{rejected}, which is on the Jellyfin 12 release line. jellyfin_k8s_image is a "
            f"10.11 build, and its loader rejects a 12.0 plugin silently behind a green "
            f"rollout — raise this anchor only when the image moves."
        )


def test_the_bump_is_never_automerged():
    """The finish is manual: the targetAbi and the digest are outside every datasource.

    The rule's POSITION is load-bearing, exactly as the five sibling rules' own descriptions
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
        f"where it stands. Append it at the end of packageRules, beside its siblings."
    )
