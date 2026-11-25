"""`lib.k8s_roles.resolved_manifest_files` derives a role's file lists.

A role that sets no `manifests_files` gets one derived from its own `templates/` and its
containers_list entry. The validator, the render corpus and the prune guards read it.

Run: uv run pytest tests/k8s/test_derived_manifest_files.py
"""

from pathlib import Path

from lib.k8s_roles import MANIFEST_TEMPLATE_GLOB, resolved_manifest_files
from _helpers import K8S_ROLES


# The deploy evaluates the Jinja list; the offline copy over-includes by design.
JINJA_LIST_ROLES = frozenset({"authelia", "navidrome", "traefik"})


def unlisted_templates(role: str, k8s_roles: Path | None = None) -> set[str]:
    """`role`'s top-level manifest templates that nothing in the role renders.

    A role that passes a list gets that list whole, so a template the list does not name is
    never rendered. `resolved_manifest_files`
    counts every basename in a Jinja value, a conditional branch included, which is the right
    reading here: the question is whether a template is named anywhere. A role's lever is held
    by a templar test in its own services test.
    """
    roles_dir = k8s_roles or K8S_ROLES
    own = {
        p.name.removesuffix(".j2")
        for p in (roles_dir / role / "templates").glob(MANIFEST_TEMPLATE_GLOB)
    }
    return own - resolved_manifest_files(role, roles_dir, {})


def test_every_template_of_a_jinja_list_role_is_listed_is_clean():
    assert {name: unlisted_templates(name) for name in JINJA_LIST_ROLES} == {
        name: set() for name in JINJA_LIST_ROLES
    }
    assert "oidc-jwk-secret.yaml" in resolved_manifest_files("authelia")
    assert "rbac.yaml" in resolved_manifest_files("traefik")


def test_a_template_an_explicit_list_leaves_out_is_flagged(tmp_path):
    _widget(tmp_path, "deployment.yaml.j2", "orphan-secret.yaml.j2")
    defaults = tmp_path / "k8s" / "roles" / "widget" / "defaults" / "main.yml"
    defaults.write_text("---\nmanifests_files: \"{{ ['deployment.yaml'] }}\"\n")
    assert unlisted_templates("widget", tmp_path / "k8s" / "roles") == {
        "orphan-secret.yaml"
    }


def _widget(tmp_path: Path, *templates: str) -> Path:
    """A playbook dir holding one role, `widget`, that ships `templates`."""
    tpl_dir = tmp_path / "k8s" / "roles" / "widget" / "templates"
    tpl_dir.mkdir(parents=True)
    (tmp_path / "k8s" / "roles" / "widget" / "defaults").mkdir()
    for name in templates:
        (tpl_dir / name).write_text("---\n")
    return tmp_path
