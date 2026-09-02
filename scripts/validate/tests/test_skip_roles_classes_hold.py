#!/usr/bin/env python3
"""The two exemption classes in validate_k8s_manifests.py must keep meaning what they say.

A role in SKIP_ROLES is never rendered and never parsed as YAML by the manifest validator, so
an entry whose stated reason has stopped being true is an unvalidated role that reads as a
deliberate decision. The set was a single literal list until 2026-08-29, and by then two of its
eight entries — volume-claim and image-builder — carried manifest templates while sitting beside
six that carried none, with nothing distinguishing the two cases mechanically.

The classes rot in OPPOSITE directions, which is why each gets its own assertion:

  - NO_MANIFEST_ROLES claims a role has no manifest template. It rots when a role grows its
    first one and is silently skipped.
  - CALLER_RENDERED_ROLES claims a role has templates that only render with caller-supplied
    vars. It rots when a role loses its templates and the exemption outlives its reason.

Run: uv run pytest scripts/validate/tests/test_skip_roles_classes_hold.py
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from validate_k8s_manifests import (
    CALLER_RENDERED_ROLES,
    K8S_ROLES,
    NO_MANIFEST_ROLES,
    SKIP_ROLES,
    is_manifest_template,
)


def manifest_templates(role: str) -> list[Path]:
    """The `.j2` files under a role's templates/ that the validator would parse as manifests.

    Same predicate the validator uses, not a second one: a helper script or a Dockerfile is not
    a manifest, and templates/config/ is one level down and deliberately out of the glob.
    """
    templates = K8S_ROLES / role / "templates"
    if not templates.is_dir():
        return []
    return sorted(p for p in templates.glob("*.j2") if is_manifest_template(p))


@pytest.mark.parametrize("role", sorted(NO_MANIFEST_ROLES))
def test_a_no_manifest_role_really_has_none(role: str):
    found = manifest_templates(role)
    assert not found, (
        f"{role} is in NO_MANIFEST_ROLES but ships {[p.name for p in found]}. Those templates "
        f"are rendered and parsed by nothing. Either move the role to CALLER_RENDERED_ROLES "
        f"with the reason its templates cannot render standalone, or drop it from both sets so "
        f"the validator covers it."
    )


@pytest.mark.parametrize("role", sorted(CALLER_RENDERED_ROLES))
def test_a_caller_rendered_role_really_has_templates(role: str):
    assert manifest_templates(role), (
        f"{role} is in CALLER_RENDERED_ROLES, which exempts it because its templates render "
        f"only with caller-supplied vars — but it has no manifest templates at all. The "
        f"exemption has outlived its reason: move it to NO_MANIFEST_ROLES or drop it."
    )


def test_the_two_classes_are_disjoint_and_cover_skip_roles():
    """The rejecting half of the split itself:

    a name in both sets would satisfy both tests above while meaning nothing, and a name in neither
    would silently stop being skipped.
    """
    assert not (NO_MANIFEST_ROLES & CALLER_RENDERED_ROLES)
    assert SKIP_ROLES == NO_MANIFEST_ROLES | CALLER_RENDERED_ROLES


def test_every_skipped_role_exists():
    """A skip for a role that was deleted or renamed is dead weight that hides the next one."""
    missing = sorted(r for r in SKIP_ROLES if not (K8S_ROLES / r).is_dir())
    assert not missing, f"SKIP_ROLES names roles that do not exist: {missing}"


def test_the_predicate_can_tell_a_manifest_from_a_helper(tmp_path: Path):
    """Without this, both parametrized tests above pass for a predicate that matches nothing."""
    assert is_manifest_template(tmp_path / "deployment.yaml.j2")
    assert not is_manifest_template(tmp_path / "Dockerfile.j2")
