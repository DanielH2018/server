"""`land_tags`' own path-to-role regex against the deployer's shared `services_from_changed_paths`.

WHY THIS TEST EXISTS RATHER THAN ONE MAPPER. `land_tags.derive` routes a changed path through
the local `role_for`/`tag_for` regex, while four other sites in the same module go through
`deploy_logic.services_from_changed_paths`. A path the two classify differently produces a
`--tags` list the deployer would not have chosen, and nothing tied them together.

WHY `derive` IS NOT SIMPLY REROUTED THROUGH THE SHARED MAPPER. They answer two different
questions, and `land_tags.py:92-125` records the difference: `role_for` names the role
directory a path sits in, and `tag_for` then keeps only the roles `containers_list` DECLARES
as a deploy tag. Eight roles under `roles/k8s/` have no entry -- `manifests` is one -- and
handing one to `--tags` makes deploy.sh refuse the WHOLE list (exit 2). The shared mapper has
no such filter, so routing `derive` through it would put `manifests` in `--tags` and refuse
every valid service beside it. That is PR #617's measured failure.

WHAT IS ASSERTED IS THEREFORE AGREEMENT AT THE ROLE LEVEL, computed from both mappers rather
than hardcoded: the role `role_for` names must be exactly the role the shared mapper
attributes the path to, for every path in the corpus. Hardcoding the expected tags would make
this test pass while the shared mapper changed underneath it, which is the drift it is here
to catch.

Run: uv run pytest scripts/deploy_tools/tests/test_land_tags_shared_mapper_agreement.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import land_tags
from deploy_logic import services_from_changed_paths

# One real path per shape the two mappers have to agree about. Real, because a made-up path
# under `ansible/roles/k8s/` would agree trivially -- these are the shapes the repo actually
# produces, including the two "no role at all" answers a broad change gives.
CORPUS = (
    # A k8s role's manifest, and the same role's tasks/: the ordinary service change.
    "ansible/roles/k8s/sonarr/templates/deployment.yaml.j2",
    "ansible/roles/k8s/sonarr/tasks/main.yml",
    # The shared k3s plane. A role with no containers_list entry, so `tag_for` drops it
    # while `role_for` and the shared mapper both still name it.
    "ansible/roles/k8s/manifests/tasks/main.yml",
    # A Docker role, which is the Pi's plane and lands in `cs.services` rather than `cs.k8s`.
    "ansible/roles/containers/wg-easy/templates/docker-compose.yml.j2",
    # The setup plane: no deploy tag exists for it at all.
    "ansible/roles/setup/gitops_deploy/files/gitops_deploy.py",
    # A shared template and shared inventory -- broad, mapping to no single service.
    "ansible/templates/traefik_labels.j2",
    "ansible/inventory/group_vars/all.yml",
    # Docs-only, and a repo script: neither is a role at all.
    "docs/python-code-organization.md",
    "scripts/deploy_tools/land.py",
)


def _shared_roles(path: str) -> set[str]:
    """The role(s) the deployer's shared mapper attributes one path to.

    The three service-bearing fields are unioned because they are one question split by
    platform and by auto-deploy eligibility: `services` is the Docker plane, `k8s` the
    cluster plane, `k8s_deploy` the image-pin subset split out of `k8s`.
    """
    cs = services_from_changed_paths([path])
    return set(cs.services) | set(cs.k8s) | set(cs.k8s_deploy)


def test_the_two_mappers_name_the_same_role_for_every_corpus_path():
    disagreements = {
        path: (land_tags.role_for(path), sorted(_shared_roles(path)))
        for path in CORPUS
        if {land_tags.role_for(path)} - {None} != _shared_roles(path)
    }
    assert not disagreements, (
        "land_tags.role_for and deploy_logic.services_from_changed_paths disagree; "
        f"path -> (role_for, shared): {disagreements}"
    )


def test_the_corpus_still_covers_a_role_and_a_non_role():
    """Non-vacuity: an all-None corpus would pass the agreement test having checked nothing."""
    named = {p for p in CORPUS if land_tags.role_for(p) is not None}
    unnamed = {p for p in CORPUS if land_tags.role_for(p) is None}
    assert len(named) >= 4, f"only {sorted(named)} still map to a role"
    assert len(unnamed) >= 4, f"only {sorted(unnamed)} still map to no role"


def test_the_agreement_check_would_catch_a_disagreement():
    """The reject half: a path only one mapper names must not compare equal."""
    only_local = "ansible/roles/k8s/sonarr/templates/deployment.yaml.j2"
    only_shared = "docs/python-code-organization.md"
    assert {land_tags.role_for(only_local)} - {None} != _shared_roles(only_shared)


def test_tag_for_is_narrower_than_role_for_and_that_is_the_reason_derive_keeps_it():
    """`manifests` is a real role and NOT a deploy tag; `--tags manifests` refuses the list."""
    shared = "ansible/roles/k8s/manifests/tasks/main.yml"
    assert land_tags.role_for(shared) == "manifests"
    assert land_tags.tag_for(shared) is None
    assert _shared_roles(shared) == {"manifests"}
