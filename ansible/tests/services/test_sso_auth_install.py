#!/usr/bin/env python3
"""The SSO-Auth plugin install step must exist, be checksum-pinned, and be loadable.

Seventh of jellyfin's plugin installers, and the one that empties the role's "unmanaged state on
the PVC" group (#1648). SSO-Auth authenticates Jellyfin users against an OIDC provider — here
Authelia, through the live `jellyfin` client that role's config declares.

**This is the only authentication layer in front of a public route** beyond Jellyfin's own local
accounts: the role runs `use_authelia: false`, so there is no forward-auth middleware on
`jellyfin.<domain>`. Dropping the plugin was the other option on #1648 and would have taken 2FA
off that route, which is why it was pinned instead.

Three things this install carries that a plain single-DLL pin does not:

- **Two dependency DLLs.** `Duende.IdentityModel.dll` and `Duende.IdentityModel.OidcClient.dll`
  ship beside `SSO-Auth.dll`, and the OIDC flow does not complete without them — an archive whose
  layout changed would install cleanly and fail every login. Media Cleaner's
  `MediaCleaner.Core.dll` is the same case.
- **Two names for one plugin.** The published manifest calls it `SSO Authentication`; it loads as
  `SSO-Auth`. Jellyfin's own installer names the directory from the MANIFEST name, and the
  ani-sync and intro-skipper containers both record `SSO Authentication_<version>` as the
  neighbouring directory they copied their layout from — so that is what the dashboard install
  wrote, and what the guard has to match to no-op on the copy already there. The sweep still
  GLOBS `SSO*_*`, which is broader on purpose: an exact name would miss a directory carrying the
  loaded name, and for an authentication provider a missed sweep means two registered against
  one route.
- **The version carries a critical SAML security fix** over 4.0.0.3, so an ageing pin is not
  merely untidy. The Renovate manager is what keeps it from ageing silently.

Webhook's shape for the manifest: the zip carries its own `meta.json`, so nothing writes one.

Run: uv run pytest ansible/tests/services/test_sso_auth_install.py
"""

import json
import re

import pytest

from lib import yaml_fast
from _helpers import ANSIBLE, REPO

DEFAULTS = ANSIBLE / "roles" / "k8s" / "jellyfin" / "defaults" / "main.yml"
DEPLOYMENT = ANSIBLE / "roles" / "k8s" / "jellyfin" / "templates" / "deployment.yaml.j2"
AUTHELIA_CONFIG = (
    ANSIBLE / "roles" / "k8s" / "authelia" / "templates" / "config-secret.yaml.j2"
)
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
    """The release tag, the asset filename and the version var must name one build.

    The version appears TWICE in this URL — `/download/v4.0.0.4/sso-authentication_4.0.0.4.zip`
    — so a rewrite that reaches only the tag produces a 404, the failure the ani-sync manager's
    description records. Factored out so the mutation test runs this logic, not a copy of it.
    """
    version = defaults["jellyfin_k8s_sso_version"]
    url = defaults["jellyfin_k8s_sso_url"]

    assert f"/download/v{version}/" in url, (
        f"jellyfin_k8s_sso_version is {version!r}, but jellyfin_k8s_sso_url does not carry it "
        f"as the release tag: {url}"
    )
    assert f"/sso-authentication_{version}.zip" in url, (
        f"jellyfin_k8s_sso_version is {version!r}, but jellyfin_k8s_sso_url does not name "
        f"sso-authentication_{version}.zip: {url}\nThe version is in this URL twice — a bump "
        f"that rewrites only the release tag resolves to a 404 and the init container fails the "
        f"pod, which for the route's only auth layer is the better failure but still an outage."
    )
    assert len(version.split(".")) == 4, (
        f"jellyfin_k8s_sso_version is {version!r}; upstream's releases are four-part "
        f"(`v4.0.0.4`), and the Renovate manager's matchStrings capture that shape."
    )


def test_the_url_names_the_pinned_version_in_both_places():
    """A bump that misses either occurrence installs the wrong build or nothing at all."""
    _assert_url_carries_the_version(_defaults())


def test_the_digest_is_the_right_shape():
    """A truncated sha256 is a pin that looks present and compares against nothing useful."""
    sha = _defaults()["jellyfin_k8s_sso_sha256"]
    assert SHA256.match(sha), (
        f"jellyfin_k8s_sso_sha256 is not 64 lowercase hex characters: {sha!r}"
    )


def test_the_plugin_target_abi_does_not_exceed_the_server():
    """The constraint the pin exists to satisfy, checked rather than remembered."""
    defaults = _defaults()
    target_abi = _version_tuple(
        defaults["jellyfin_k8s_sso_target_abi"], "jellyfin_k8s_sso_target_abi"
    )
    image = defaults["jellyfin_k8s_image"]
    server = _version_tuple(image.rsplit(":", 1)[-1], "the jellyfin image tag")

    abi, srv = _padded(target_abi, server)
    assert abi <= srv, (
        f"SSO-Auth {defaults['jellyfin_k8s_sso_version']} targets Jellyfin "
        f"{defaults['jellyfin_k8s_sso_target_abi']}, but jellyfin_k8s_image is {image}.\n"
        f"Jellyfin's loader rejects a plugin built for a newer server, silently — the rollout "
        f"stays green, GET /Plugins omits it, and the OIDC sign-in button simply stops "
        f"rendering. That is #1648's whole failure mode, now with a pin behind it."
    )


def test_authelia_still_declares_the_oidc_client_this_plugin_authenticates_against():
    """WHY the call on #1648 was keep rather than drop, asserted rather than remembered.

    The plugin is not optional decoration: authelia carries a `jellyfin` OIDC client whose
    redirect URI is this plugin's own callback path. Dropping the plugin would orphan that
    client and take 2FA off a public route; removing the client would break the plugin. Either
    edit made alone is the thing this asserts against.
    """
    config = AUTHELIA_CONFIG.read_text()
    assert "/sso/OID/redirect/authelia" in config, (
        f"{AUTHELIA_CONFIG.name} no longer declares a redirect URI on jellyfin's "
        f"/sso/OID/redirect/ path. That path IS the SSO-Auth plugin's callback — if the OIDC "
        f"client was removed deliberately, this install and the role's CLAUDE.md census have to "
        f"move with it, because the plugin is the only auth layer on a public route."
    )


def _assert_install_step(template: str, version: str) -> None:
    """Everything the rendered deployment must carry for the install to work.

    Factored out so the same assertions can run against a MUTATED template. A guard that is only
    ever handed the real file is a guard nobody has seen fail.
    """
    assert "- name: install-sso-auth" in template, (
        "the deployment no longer declares the install-sso-auth init container"
    )
    assert "{{ jellyfin_k8s_sso_sha256 }}" in template, (
        "the install-sso-auth init container no longer templates jellyfin_k8s_sso_sha256 — an "
        "unpinned download of the plugin that authenticates a public route is whatever the "
        "release asset has been replaced with"
    )
    assert "if got != WANT_SHA256:" in template, (
        "the install-sso-auth init container downloads the archive but no longer COMPARES its "
        "digest — the pin is present and inert"
    )
    assert "{{ jellyfin_k8s_sso_version }}" in template, (
        "the install-sso-auth init container no longer templates jellyfin_k8s_sso_version — the "
        "install marker would stop tracking the pin"
    )
    assert version not in template, (
        f"the version {version!r} is written literally into {DEPLOYMENT.name}. Take it from "
        f"jellyfin_k8s_sso_version instead."
    )
    assert 'PLUGINS = Path("/config/data/plugins")' in template, (
        "the install-sso-auth init container no longer targets /config/data/plugins. Jellyfin "
        "scans only that directory — installing anywhere else reports success and leaves the "
        "plugin unloaded, behind a green rollout."
    )
    assert 'PLUGIN_DIR = PLUGINS / ("SSO Authentication_" + VERSION)' in template, (
        "the plugin directory no longer uses the MANIFEST name. Jellyfin's own installer writes "
        "`<manifest name>_<version>`, and the ani-sync and intro-skipper containers both record "
        "seeing exactly that on this volume — so this is the name that matches the "
        "dashboard-installed copy. `SSO-Auth`, the name the plugin LOADS under, is a different "
        "string and the guard would miss the copy already there, reinstalling on every pod "
        "start."
    )
    assert 'DLL = "SSO-Auth.dll"' in template, (
        "the install-sso-auth init container no longer checks for the plugin's own DLL"
    )
    assert (
        'DEP_DLLS = ("Duende.IdentityModel.dll", "Duende.IdentityModel.OidcClient.dll")'
        in template
    ), (
        "the install-sso-auth init container no longer checks for the two Duende DLLs. The OIDC "
        "flow does not complete without them, so an archive whose layout changed would install "
        "cleanly and fail every sign-in."
    )
    assert "for needed in (DLL,) + DEP_DLLS:" in template, (
        "the install-sso-auth init container names all three DLLs but no longer LOOPS over "
        "them — two of the three checks would be dead"
    )
    assert 'PLUGINS.glob("SSO*_*")' in template, (
        "the install-sso-auth sweep no longer globs. It is deliberately broader than the "
        "directory name above, because this plugin's manifest name and its loaded name differ "
        "and a dashboard install could have written either — and a sweep that misses the old "
        "directory leaves two authentication providers registered for one route."
    )


def test_the_rendered_deployment_carries_the_pinned_install_step():
    _assert_install_step(
        DEPLOYMENT.read_text(), _defaults()["jellyfin_k8s_sso_version"]
    )


@pytest.mark.parametrize(
    ("what", "victim"),
    [
        ("the checksum comparison", "if got != WANT_SHA256:"),
        ("the checksum itself", "{{ jellyfin_k8s_sso_sha256 }}"),
        ("the whole init container", "- name: install-sso-auth"),
        ("the plugin DLL check", 'DLL = "SSO-Auth.dll"'),
        (
            "the dependency DLL check",
            'DEP_DLLS = ("Duende.IdentityModel.dll", "Duende.IdentityModel.OidcClient.dll")',
        ),
        ("the loop over all three DLLs", "for needed in (DLL,) + DEP_DLLS:"),
        (
            "the plugin directory layout",
            'PLUGIN_DIR = PLUGINS / ("SSO Authentication_" + VERSION)',
        ),
        ("the broad sweep glob", 'PLUGINS.glob("SSO*_*")'),
    ],
)
def test_the_guard_rejects_a_template_missing_the_step(what, victim):
    """The red half. Each removal above is a real way this install goes quietly wrong."""
    mutated = DEPLOYMENT.read_text().replace(victim, "")
    assert victim not in mutated, (
        f"the mutation for {what} matched nothing — fix the fixture"
    )

    with pytest.raises(AssertionError):
        _assert_install_step(mutated, _defaults()["jellyfin_k8s_sso_version"])


@pytest.mark.parametrize(
    ("what", "before", "after"),
    [
        (
            "a release tag left behind by a bump",
            "/download/v4.0.0.4/sso-authentication_4.0.0.4.zip",
            "/download/v4.0.0.4/sso-authentication_4.0.0.5.zip",
        ),
        (
            "an asset name left behind by a bump",
            "/download/v4.0.0.4/sso-authentication_4.0.0.4.zip",
            "/download/v4.0.0.5/sso-authentication_4.0.0.4.zip",
        ),
    ],
)
def test_the_pin_guard_rejects_a_mismatched_defaults_file(what, before, after):
    """The red half for the pin, and specifically for the half-rewritten URL.

    Both mutations are what a partial bump really produces: one resolves to a 404, the other
    installs the old build under the new marker.
    """
    text = DEFAULTS.read_text()
    assert before in text, f"the mutation for {what} matched nothing — fix the fixture"
    mutated = yaml_fast.safe_load(text.replace(before, after, 1))
    mutated["jellyfin_k8s_sso_version"] = "4.0.0.5"

    with pytest.raises(AssertionError):
        _assert_url_carries_the_version(mutated)


# ── Renovate coverage (#1557) ───────────────────────────────────────────────────────────────
# renovate.json's k8s-images manager keys on `_image:`, so a plugin version, its URL and its
# checksum are invisible to every manager without a dedicated one — and a pin with no update
# signal reads exactly like a plugin with no updates available. For this plugin that also means
# no signal for the next SAML-class security fix.

DEP = "9p4/jellyfin-plugin-sso"


def _renovate() -> dict:
    return json.loads(RENOVATE.read_text())


def _manager() -> dict:
    manager = next(
        (m for m in _renovate()["customManagers"] if m.get("depNameTemplate") == DEP),
        None,
    )
    assert manager, (
        f"renovate.json no longer carries a customManager for {DEP} — the SSO-Auth pin ages "
        f"with no update signal, and 4.0.0.4 was itself a critical SAML security fix"
    )
    return manager


def test_every_manager_pattern_still_matches_the_pinned_release():
    """A manager whose matchStrings match nothing is inert, and inert looks like up-to-date.

    All THREE patterns must capture the same version: the release tag, the asset filename and
    the version var. Renovate rewrites them in step, and a rewrite reaching only some of them
    is the 404 the ani-sync manager's description records.
    """
    text = DEFAULTS.read_text()
    version = _defaults()["jellyfin_k8s_sso_version"]
    patterns = _manager()["matchStrings"]

    assert len(patterns) == 3, (
        f"the SSO-Auth Renovate manager has {len(patterns)} matchStrings. The version occurs "
        f"three times in {DEFAULTS.name} — the URL's release tag, the URL's asset filename and "
        f"jellyfin_k8s_sso_version — and every occurrence needs one, or a bump leaves the pin "
        f"internally inconsistent."
    )
    for pattern in patterns:
        found = re.findall(pattern.replace("(?<", "(?P<"), text)
        assert found, (
            f"the SSO-Auth Renovate manager's matchString {pattern!r} matches nothing in "
            f"{DEFAULTS.name} — the manager is inert, which reads as 'no updates available'"
        )
        assert set(found) == {version}, (
            f"the SSO-Auth Renovate manager's matchString {pattern!r} captured "
            f"{sorted(set(found))} in {DEFAULTS.name}, not the pinned version {version!r}"
        )


def test_the_manager_admits_the_pinned_tag():
    """A version template that cannot parse the pin offers nothing, including the pin itself."""
    manager = _manager()
    version = _defaults()["jellyfin_k8s_sso_version"]
    anchor = re.compile(manager["extractVersionTemplate"].replace("(?<", "(?P<"))

    assert anchor.match(f"v{version}"), (
        f"the SSO-Auth Renovate manager's extractVersionTemplate {anchor.pattern!r} does not "
        f"admit the pinned tag v{version} — the manager offers nothing at all"
    )
    versioning = re.compile(
        manager["versioningTemplate"].removeprefix("regex:").replace("(?<", "(?P<")
    )
    # Upstream's history is four-part throughout (`v3.5.2.4`, `v4.0.0.3`, `v4.0.0.4`), and the
    # fourth group must be `build` rather than `revision` for the reason the intro-skipper
    # manager records — Renovate reads `revision` only when `build` is already present, so
    # `revision` alone leaves every 4.0.0.x equal and the manager inert.
    assert versioning.match(version) and versioning.match("3.5.2.4"), (
        f"the SSO-Auth Renovate manager's versioningTemplate "
        f"{manager['versioningTemplate']!r} does not parse upstream's four-part tags — an "
        f"unorderable version is silently skipped"
    )
    assert "(?<build>" in manager["versioningTemplate"], (
        "the SSO-Auth Renovate manager's versioningTemplate has no `build` group. Every "
        "upstream release differs only in the FOURTH segment, and Renovate reads `revision` "
        "only when `build` is already present — so without it every 4.0.0.x compares equal and "
        "the manager offers nothing."
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
        f"automerging `k8s image jellyfin` group and ship a half-finished pin for the plugin "
        f"that authenticates a public route"
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
