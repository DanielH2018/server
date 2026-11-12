"""`lib.k8s_roles.resolved_manifest_files` derives a role's file lists.

A role that sets no `manifests_files` gets one derived from its own `templates/` and its
containers_list entry (#3662). The validator, the render corpus and the prune guards read it.

Run: uv run pytest ansible/tests/k8s/test_derived_manifest_files.py
"""

from pathlib import Path

import pytest

from lib.k8s_roles import (
    MANIFEST_TEMPLATE_GLOB,
    SECRET_MANIFEST_NAME,
    resolved_manifest_files,
)
from lib import yaml_fast
from _helpers import ANSIBLE
from _k8s_render import rendered_texts


# Roles whose include builds a list with a Jinja expression over role vars. The deploy
# evaluates it; the offline copy reads every basename in it, over-inclusively by design, so the
# two are not comparable and these roles are left out of the agreement test by name.
JINJA_LIST_ROLES = frozenset({"authelia", "navidrome", "traefik"})


def unlisted_templates(role: str, k8s_roles: Path | None = None) -> set[str]:
    """`role`'s top-level manifest templates that nothing in the role renders.

    A role that passes a list gets that list whole, so a template neither list names is never
    rendered. `resolved_manifest_files`
    counts every basename in a Jinja value, a conditional branch included, which is the right
    reading here: the question is whether a template is named anywhere. A role's lever is held
    by a templar test in its own services test.
    """
    roles_dir = k8s_roles or ANSIBLE / "roles" / "k8s"
    own = {
        p.name.removesuffix(".j2")
        for p in (roles_dir / role / "templates").glob(MANIFEST_TEMPLATE_GLOB)
    }
    files, secret = resolved_manifest_files(role, roles_dir, {})
    return own - files - secret


def test_every_template_of_a_jinja_list_role_is_listed_is_clean():
    assert {name: unlisted_templates(name) for name in JINJA_LIST_ROLES} == {
        name: set() for name in JINJA_LIST_ROLES
    }
    # The named members: authelia's OIDC key Secret, which R2's PR 7 lists behind its lever,
    # and traefik's rbac.yaml, which its flip moved from a staging task into the list.
    _files, secret = resolved_manifest_files("authelia")
    assert "oidc-jwk-secret.yaml" in secret
    assert "rbac.yaml" in resolved_manifest_files("traefik")[0]


def test_a_template_an_explicit_list_leaves_out_is_flagged(tmp_path):
    _widget(tmp_path, "deployment.yaml.j2", "orphan-secret.yaml.j2")
    defaults = tmp_path / "roles" / "k8s" / "widget" / "defaults" / "main.yml"
    defaults.write_text(
        "---\n"
        "manifests_files: \"{{ ['deployment.yaml'] }}\"\n"
        "manifests_secret_files: '{{ [] }}'\n"
    )
    assert unlisted_templates("widget", tmp_path / "roles" / "k8s") == {
        "orphan-secret.yaml"
    }


def _widget(tmp_path: Path, *templates: str) -> Path:
    """A playbook dir holding one role, `widget`, that ships `templates`."""
    tpl_dir = tmp_path / "roles" / "k8s" / "widget" / "templates"
    tpl_dir.mkdir(parents=True)
    (tmp_path / "roles" / "k8s" / "widget" / "defaults").mkdir()
    for name in templates:
        (tpl_dir / name).write_text("---\n")
    return tmp_path


# The fail-safe for deriving secrecy from a name. A template that renders a Secret but is not
# named as one renders 0644 and outside no_log, with its decrypted values in the task output.
def world_readable_secrets(renders, resolve) -> list[str]:
    """`<role>/<template>` for every render that holds a Secret yet stages world-readable.

    `renders` yields `(role, template name, rendered text)`; `resolve(role)` returns the role's
    `(files, secret_files)`.
    """
    found = []
    for role, name, text in renders:
        if name.removesuffix(".j2") not in resolve(role)[0]:
            continue
        docs = [d for d in yaml_fast.safe_load_all(text) if isinstance(d, dict)]
        if any(d.get("kind") == "Secret" for d in docs):
            found.append(f"{role}/{name}")
    return sorted(found)


def test_no_role_renders_a_secret_world_readable_is_clean():
    renders = rendered_texts()
    assert {
        ("cluster-base", "readonly-rbac.yaml.j2"),
        ("sonarr", "secret-exportarr.yaml.j2"),
    } <= {(role, name) for role, name, _text in renders}, (
        "the render corpus no longer carries the two Secret templates this guard is sized on"
    )
    assert world_readable_secrets(renders, resolved_manifest_files) == []


def test_a_secret_template_named_as_a_manifest_is_flagged():
    renders = [
        ("widget", "monitors.yaml.j2", "---\napiVersion: v1\nkind: Secret\n"),
        ("widget", "secret.yaml.j2", "---\napiVersion: v1\nkind: Secret\n"),
    ]

    def resolve(_role):
        return {"monitors.yaml"}, {"secret.yaml"}

    assert world_readable_secrets(renders, resolve) == ["widget/monitors.yaml.j2"]


@pytest.mark.parametrize(
    ("name", "is_secret"),
    [
        ("secret.yaml", True),
        ("config-secret.yaml", True),
        ("secret-exportarr.yaml", True),
        ("secrets-ui.yaml", False),
        ("deployment.yaml", False),
    ],
)
def test_the_secret_name_rule(name, is_secret):
    assert bool(SECRET_MANIFEST_NAME.search(name)) is is_secret
