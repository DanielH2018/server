#!/usr/bin/env python3
"""The Trakt plugin must stay removed, and the removal must reach the PVC.

Trakt was the sixth plugin this role installed (#1617) and was removed at the operator's request
on 2026-09-10. Dropping the install container alone would not have removed it: the install
wrote `/config/data/plugins/Trakt_30.0.0.0` onto the `jellyfin-config` PVC, Jellyfin scans that
directory on every start, and the read-only ServiceAccount cannot exec into the pod. An init
container is the only write path this repo has to the PVC, so the uninstall is one too.

Two halves, each with a red proof:

- **The sweep exists and names the plugin.** A `remove-trakt` init container globs `Trakt_*`
  under the plugins directory. A template with the container deleted, or with the glob renamed
  out from under it, must fail — otherwise the plugin silently reloads on the next restart.
- **Nothing reinstalls it.** No `install-trakt` container and no `jellyfin_k8s_trakt_*` var
  survive in the role, and Renovate carries no manager for it — a manager left behind would
  open bump PRs against a var that no longer exists.

Run: uv run pytest ansible/tests/services/test_trakt_removed.py
"""

import json
import re

import pytest

from _helpers import ANSIBLE, REPO

DEFAULTS = ANSIBLE / "roles" / "k8s" / "jellyfin" / "defaults" / "main.yml"
DEPLOYMENT = ANSIBLE / "roles" / "k8s" / "jellyfin" / "templates" / "deployment.yaml.j2"
RENOVATE = REPO / "renovate.json"

SWEEP = 'PLUGINS.glob("Trakt_*")'
CONTAINER = "- name: remove-trakt"


def _assert_removal_step(template: str) -> None:
    assert CONTAINER in template, (
        "the deployment no longer declares the remove-trakt init container. The plugin directory "
        "the #1617 install wrote is still on the jellyfin-config PVC until something deletes it, "
        "and this container is the repo's only write path there."
    )
    body = template.split(CONTAINER, 1)[1].split("- name: ", 1)[0]
    assert SWEEP in body, (
        "the remove-trakt init container no longer globs Trakt_* under /config/data/plugins. "
        "The directory takes the name the zip's meta.json declares, so a glob on any other "
        "name sweeps nothing and Jellyfin loads the plugin again on the next start."
    )
    assert 'Path("/config/data/plugins")' in body, (
        "the remove-trakt init container no longer targets /config/data/plugins, the only "
        "directory Jellyfin scans."
    )


def test_the_deployment_carries_the_removal_step():
    _assert_removal_step(DEPLOYMENT.read_text())


@pytest.mark.parametrize(
    ("what", "victim"),
    [
        ("the whole init container", CONTAINER),
        ("the plugin directory sweep", SWEEP),
        ("the plugins directory", 'Path("/config/data/plugins")'),
    ],
)
def test_the_guard_rejects_a_template_missing_the_step(what, victim):
    """Red proof: a template with the step cut out must fail, not pass vacuously."""
    template = DEPLOYMENT.read_text()
    # Cut inside the remove-trakt container, not at the first match in the file: the five
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
    assert "- name: install-trakt" not in template, (
        "an install-trakt init container is back in the deployment. The plugin was removed on "
        "2026-09-10 (#1617, #1664); reinstalling it is a decision, not a merge accident — and "
        "it would race the remove-trakt container that runs beside it."
    )
    leftovers = re.findall(
        r"^jellyfin_k8s_trakt_\w+:", DEFAULTS.read_text(), re.MULTILINE
    )
    assert not leftovers, (
        f"defaults/main.yml still declares {leftovers}. Nothing reads them since the removal, "
        f"and a stale pin is exactly what the Renovate check below would then chase."
    )


def test_renovate_carries_no_manager_for_it():
    config = json.loads(RENOVATE.read_text())
    managers = [
        m
        for m in config.get("customManagers", [])
        if "trakt" in m.get("depNameTemplate", "").lower()
    ]
    rules = [
        r
        for r in config.get("packageRules", [])
        if any("trakt" in name.lower() for name in r.get("matchPackageNames", []))
    ]
    assert not managers and not rules, (
        f"renovate.json still names the Trakt plugin: managers={managers!r} rules={rules!r}. "
        f"The pin it tracked is gone, so a surviving manager matches nothing and a surviving "
        f"rule guards nothing — both read as coverage that is not there."
    )
