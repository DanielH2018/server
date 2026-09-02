"""`_BUILD_ROLL_COUPLINGS` must still name every split build role, and only those.

A build role that renders no workload of its own pushes an image some OTHER role runs. The
`k8s_rebuilt_images` fact that turns a rebuild into a rollout is play-scoped, so building
without deploying the consumer leaves the old pods up and reports green -- the 2026-08-08
`@n8n/di` failure, recorded in roles/k8s/n8n/defaults/main.yml.

The constant is hand-written because the population is ONE. This re-derives that population
from the role sources, so adding a second split build role fails here instead of silently
inheriting the bug. That is the whole point: the risk is not that the current entry is wrong,
it is that a future role joins the class unnoticed.

Run: uv run pytest ansible/tests/deploy/test_build_roll_couplings.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from _helpers import REPO

sys.path.insert(
    0, str(REPO / "ansible" / "roles" / "setup" / "gitops_deploy" / "files")
)

from deploy_logic import (
    _BUILD_ROLL_COUPLINGS,
    expand_build_couplings,
)

K8S_ROLES = REPO / "ansible" / "roles" / "k8s"

# Manifest kinds that run a workload. A role rendering one of these deploys what it builds in
# the same role, so a single tag covers build and roll and the play-scoped fact never has to
# cross a role boundary.
_WORKLOAD_KINDS = ("deployment", "daemonset", "statefulset", "cronjob", "job")

# image-builder is the shared build machinery itself, not a service that builds one.
_SHARED = frozenset({"image-builder"})


def _builds_an_image(role: Path) -> bool:
    tasks = role / "tasks" / "main.yml"
    return tasks.is_file() and "image_builder_name" in tasks.read_text()


def _renders_a_workload(role: Path) -> bool:
    templates = role / "templates"
    if not templates.is_dir():
        return False
    return any(
        name.startswith(_WORKLOAD_KINDS)
        for name in (p.name.lower() for p in templates.iterdir())
    )


def _split_build_roles() -> set[str]:
    """Roles that build an image and render no workload to run it."""
    return {
        role.name
        for role in K8S_ROLES.iterdir()
        if role.is_dir()
        and role.name not in _SHARED
        and _builds_an_image(role)
        and not _renders_a_workload(role)
    }


def test_every_split_build_role_declares_its_consumer():
    missing = _split_build_roles() - set(_BUILD_ROLL_COUPLINGS)
    assert not missing, (
        f"build roles that render no workload and declare no consumer: {sorted(missing)}. "
        "Deploying one alone builds an image nothing rolls onto. Add it to "
        "_BUILD_ROLL_COUPLINGS with the role that runs what it builds."
    )


def test_no_coupling_names_a_role_that_deploys_itself():
    """The reject half of the census.

    A coupling on a role that renders its own workload is dead weight, and would hide the fact that
    the class has changed shape.
    """
    stale = set(_BUILD_ROLL_COUPLINGS) - _split_build_roles()
    assert not stale, (
        f"couplings for roles that already deploy themselves: {sorted(stale)}"
    )


def test_the_census_actually_finds_build_roles():
    """A discriminator that matched nothing would make both tests above vacuously true."""
    assert _split_build_roles(), (
        "found no split build roles at all — the census is broken"
    )


def test_every_coupling_target_is_a_real_role():
    for build_role, needs in _BUILD_ROLL_COUPLINGS.items():
        for target in needs:
            assert (K8S_ROLES / target).is_dir(), (
                f"{build_role} names '{target}', which is not a role under roles/k8s/"
            )


def test_the_build_role_runs_before_its_consumer():
    """Tags do not reorder anything.

    `containers_list` runs in list order with no toposort, so a consumer listed BEFORE its build
    role reads `k8s_rebuilt_images` while the fact is still empty -- the expanded tag set would be
    correct and the rollout would still miss the rebuild. Assert the order, or the fix ships a green
    run that changes nothing.
    """
    text = (REPO / "ansible" / "inventory" / "host_vars" / "daniel-box.yml").read_text()
    for build_role, needs in _BUILD_ROLL_COUPLINGS.items():
        build_at = text.find(f"name: {build_role}\n")
        assert build_at != -1, f"{build_role} is not in daniel-box's containers_list"
        for target in needs:
            target_at = text.find(f"name: {target}\n")
            assert target_at != -1, f"{target} is not in daniel-box's containers_list"
            assert build_at < target_at, (
                f"'{target}' is listed before '{build_role}', so the rollout runs before the "
                "rebuild populates k8s_rebuilt_images. Move the build role earlier."
            )


def test_expansion_adds_the_consumer():
    assert expand_build_couplings({"n8n-images"}) == {"n8n-images", "n8n"}


def test_expansion_leaves_an_unrelated_tag_alone():
    """The reject half. An expansion that widened everything would pass the test above."""
    assert expand_build_couplings({"sonarr"}) == {"sonarr"}


def test_expansion_is_one_directional():
    """Editing the workload's manifests needs no rebuild — the manifest change rolls on its own.

    Widening that direction would rebuild an image on every manifest edit.
    """
    assert expand_build_couplings({"n8n"}) == {"n8n"}
