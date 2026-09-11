#!/usr/bin/env python3
"""The SSO-Auth plugin must stay removed, and the removal must reach the PVC and Authelia.

SSO-Auth was installed under #1648 and removed at the operator's request on 2026-09-10. #1674
identified it as the blocker on any move to the Jellyfin 12 image line: upstream
9p4/jellyfin-plugin-sso publishes no 12 build, and its newest release (4.0.0.4) still declares
targetAbi 10.11.0.0.

Dropping the install container alone would not have removed it. The install wrote
`SSO Authentication_<version>` onto the `jellyfin-config` PVC, Jellyfin scans that directory on
every start, and the read-only ServiceAccount cannot exec into the pod -- an init container is
this repo's only write path there. That is Trakt's removal shape, and this follows it.

The third half is what makes this plugin different from Trakt. It was the ONLY authentication
layer in front of a public route: the jellyfin role runs `use_authelia: false`, and Authelia
carried a `jellyfin` OIDC client whose redirect_uri only this plugin served. A client left
behind would point at a path nothing answers, so its retirement is pinned here rather than
trusted to memory.

Each half carries a red proof, because a guard that is only ever observed passing is not
evidence it can fail.

Run: uv run pytest ansible/tests/services/test_sso_auth_removed.py
"""

import json
import re

import pytest

from _helpers import ANSIBLE, REPO

DEFAULTS = ANSIBLE / "roles" / "k8s" / "jellyfin" / "defaults" / "main.yml"
DEPLOYMENT = ANSIBLE / "roles" / "k8s" / "jellyfin" / "templates" / "deployment.yaml.j2"
AUTHELIA_CONFIG = (
    ANSIBLE / "roles" / "k8s" / "authelia" / "templates" / "config-secret.yaml.j2"
)
RENOVATE = REPO / "renovate.json"

# `SSO*_*`, not the exact manifest name. This plugin is the one whose manifest name ("SSO
# Authentication") differs from the name it loads under ("SSO-Auth"), so a dashboard install may
# have written either -- the install container's own sweep was broad for the same reason.
SWEEP = 'PLUGINS.glob("SSO*_*")'
CONFIG_SWEEP = '"SSO-Auth.xml"'
CONTAINER = "- name: remove-sso-auth"


def _assert_removal_step(template: str) -> None:
    assert CONTAINER in template, (
        "the deployment no longer declares the remove-sso-auth init container. The plugin "
        "directory the #1648 install wrote is still on the jellyfin-config PVC until something "
        "deletes it, and this container is the repo's only write path there."
    )
    body = template.split(CONTAINER, 1)[1].split("- name: ", 1)[0]
    assert SWEEP in body, (
        "the remove-sso-auth init container no longer globs SSO*_* under /config/data/plugins. "
        "The glob is broad on purpose: the manifest name and the loaded name differ, so a glob "
        "on either exact name leaves the other behind and Jellyfin reloads the plugin."
    )
    assert CONFIG_SWEEP in body, (
        "the remove-sso-auth init container no longer deletes SSO-Auth.xml. It is keyed by the "
        "plugin's CLASS name and lives OUTSIDE every plugin directory, so the glob above cannot "
        "reach it -- it would survive as OIDC provider settings configuring nothing."
    )
    assert 'Path("/config/data/plugins")' in body, (
        "the remove-sso-auth init container no longer targets /config/data/plugins, the only "
        "directory Jellyfin scans."
    )


def test_the_deployment_carries_the_removal_step():
    _assert_removal_step(DEPLOYMENT.read_text())


@pytest.mark.parametrize(
    ("what", "victim"),
    [
        ("the whole init container", CONTAINER),
        ("the plugin directory sweep", SWEEP),
        ("the configuration-file sweep", CONFIG_SWEEP),
        ("the plugins directory", 'Path("/config/data/plugins")'),
    ],
)
def test_the_guard_rejects_a_template_missing_the_step(what, victim):
    """Red proof: a template with the step cut out must fail, not pass vacuously."""
    template = DEPLOYMENT.read_text()
    # Cut inside the remove-sso-auth container, not at the first match in the file: the
    # installers above it share the plugins-directory line, and cutting one of theirs would
    # leave this container intact and the "red proof" green.
    head, tail = template.split(CONTAINER, 1)
    step = CONTAINER + tail
    assert victim in step, f"fixture drift: {what} is not in the step to begin with"
    mutated = head + step.replace(victim, "", 1)
    with pytest.raises(AssertionError):
        _assert_removal_step(mutated)


def test_nothing_in_the_role_installs_it_again():
    template = DEPLOYMENT.read_text()
    assert "- name: install-sso-auth" not in template, (
        "an install-sso-auth init container is back in the deployment. The plugin was removed "
        "on 2026-09-10 (#1674) and it has no Jellyfin 12 build; reinstalling it is a decision, "
        "not a merge accident -- and it would race the remove-sso-auth container beside it."
    )
    leftovers = re.findall(
        r"^jellyfin_k8s_sso_\w+:", DEFAULTS.read_text(), re.MULTILINE
    )
    assert not leftovers, (
        f"defaults/main.yml still declares {leftovers}. Nothing reads them since the removal, "
        f"and a stale pin is what the Renovate check below would then chase."
    )


def _config_without_comments(text: str) -> str:
    """The template's active lines only.

    The removal left an explanatory comment naming both the client and its redirect path, which
    is the right place for that history -- but a substring check over the whole file would then
    match the prose and fail on a correct config. Assert against what Authelia parses.
    """
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def test_authelia_carries_no_client_for_it():
    """The half Trakt did not have: an OIDC client outliving the only thing that redeemed it."""
    config = _config_without_comments(AUTHELIA_CONFIG.read_text())
    assert "client_id: 'jellyfin'" not in config, (
        "authelia still declares the `jellyfin` OIDC client. Its redirect_uri is served only by "
        "the SSO-Auth plugin, which is removed -- the client now has no caller, and a client "
        "that cannot be redeemed is an authentication path that reads as configured."
    )
    assert "/sso/OID/redirect/authelia" not in config, (
        "authelia still names the SSO-Auth redirect path. Nothing serves it once the plugin is "
        "swept off the PVC."
    )


def test_the_comment_stripper_still_sees_a_real_client():
    """Red proof for `_config_without_comments`.

    Stripping comments is what lets the assertion above coexist with the prose recording the
    removal. It must not also strip an ACTIVE declaration -- a stripper that swallowed real
    config would turn this guard green over a live `jellyfin` client, which is the one outcome
    it exists to prevent.
    """
    revived = AUTHELIA_CONFIG.read_text().replace(
        "        clients:\n",
        "        clients:\n          - client_id: 'jellyfin'\n",
        1,
    )
    assert "client_id: 'jellyfin'" in _config_without_comments(revived)


def test_renovate_carries_no_manager_for_it():
    config = json.loads(RENOVATE.read_text())
    managers = [
        m
        for m in config.get("customManagers", [])
        if "sso" in m.get("depNameTemplate", "").lower()
    ]
    rules = [
        r
        for r in config.get("packageRules", [])
        if any("sso" in name.lower() for name in r.get("matchPackageNames", []))
    ]
    assert not managers and not rules, (
        f"renovate.json still names the SSO-Auth plugin: managers={managers!r} rules={rules!r}. "
        f"The pin it tracked is gone, so a surviving manager matches nothing and a surviving "
        f"rule guards nothing -- both read as coverage that is not there."
    )
