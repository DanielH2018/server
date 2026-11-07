"""Tripwire for the build-roll coupling that was deleted as memberless.

The coupling was a WIDENING: it added something a narrower derivation structurally cannot see,
so its absence turns a loud, wrong answer into a quiet, narrow one. Git history carries the
mechanism. This file owns the day a member of the class reappears.

The test is keyed on the SHAPE rather than on the name of the member that used to be in it. It
reads the live tree and asserts it is clean today, and a planted fixture tree trips it
(`.claude/rules/python-layout.md`). The failure message names the `git show` that restores the
mechanism, so whoever trips it restores rather than re-derives.

Run: uv run pytest ansible/tests/deploy/test_deleted_widening_tripwires.py
"""

from pathlib import Path

from lib.k8s_roles import role_callers

from _helpers import REPO

# The commit that deleted the coupling. Named in the failure message below.
DELETED_IN = "fb9e4017f"

# `_BUILD_ROLL_COUPLINGS` mapped a build role that renders no workload of its own to the role
# that runs what it builds, so deriving the build role from a changed path also derived its
# consumer. Without it a Renovate Dockerfile bump derives the build role alone: the renderer
# gives a tag the content tags of its own role's builds only, so the consumer's manifest does
# not move, nothing rolls, and the landing reports green.

# Manifest kinds that run a workload. A role rendering one of these deploys what it builds in
# the same role, so a single tag covers build and roll.
_WORKLOAD_KINDS = ("deployment", "daemonset", "statefulset", "cronjob", "job")

BUILDER = "image-builder"


def _renders_a_workload(role: Path) -> bool:
    templates = role / "templates"
    if not templates.is_dir():
        return False
    return any(
        name.startswith(_WORKLOAD_KINDS)
        for name in (p.name.lower() for p in templates.iterdir())
    )


def split_build_roles(repo: Path = REPO) -> set[str]:
    """Roles that include `k8s/image-builder` and render no workload to run what it builds.

    Keyed on the edge `role_callers` reports rather than on a mention of `image_builder_name`, which is what
    excludes `image-builder` itself: the shared build machinery does not include itself, and its
    own `build-job.yaml.j2` is the build Job, not a workload the class is about.
    """
    callers = role_callers(repo).get(BUILDER) or set()
    k8s_roles = Path(repo) / "ansible" / "roles" / "k8s"
    return {name for name in callers if not _renders_a_workload(k8s_roles / name)}


def test_no_k8s_role_builds_an_image_and_renders_no_workload_is_clean():
    found = split_build_roles()
    assert not found, (
        f"these roles include k8s/{BUILDER} and render no workload manifest of their own: "
        f"{sorted(found)}. Deploying one alone builds an image nothing rolls onto, because "
        "the render gives a tag only its own builds' content tags. The build-roll coupling that derived the consumer "
        f"alongside the build role was deleted as memberless in #2876; restore it with "
        f"`git show {DELETED_IN}^:ansible/tests/deploy/test_build_roll_couplings.py` and "
        f"`git show {DELETED_IN}` (the coupling reached about a dozen land and deploy call sites)."
    )


def test_a_role_that_builds_and_renders_no_workload_is_flagged(tmp_path):
    """The red proof: a planted role joining the class trips the census."""
    _plant_k8s_role(tmp_path, "widget-images", includes=BUILDER, templates=[])
    _plant_k8s_role(
        tmp_path, "widget", includes=BUILDER, templates=["deployment.yaml.j2"]
    )
    assert split_build_roles(tmp_path) == {"widget-images"}


def test_the_build_role_census_is_not_empty():
    """Non-vacuity: a renamed defaults key would empty the callers and pass the clean test."""
    callers = role_callers(REPO).get(BUILDER) or set()
    assert {"n8n", "valheim"} <= callers, (
        f"role_callers no longer names n8n and valheim as callers of {BUILDER}: "
        f"{sorted(callers)}, so the clean test above may be passing over nothing"
    )


def _plant_k8s_role(root: Path, name: str, includes: str = "", templates=()):
    """Write a minimal `ansible/roles/k8s/<name>/` under `root` for the red proof."""
    role = root / "ansible" / "roles" / "k8s" / name
    role.mkdir(parents=True)
    if includes == BUILDER:
        (role / "defaults").mkdir()
        (role / "defaults" / "main.yml").write_text(
            "image_builder_builds:\n  - image_builder_name: widget\n"
        )
    if templates:
        (role / "templates").mkdir()
        for template in templates:
            (role / "templates" / template).write_text("{}\n")
