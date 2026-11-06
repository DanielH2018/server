"""`targets.render_targets` reads no task file, and lists what the task-reading predicate lists.

Run: uv run pytest scripts/renderer/tests/test_renderer_targets.py
"""

from pathlib import Path

import pytest

from deploy_tools.render_targets import includes_manifests
from lib.ansible_inventory import K8S_HOST
from lib.repo_paths import HOST_VARS, K8S_ROLES
from lib.render_guard import containers_entries, entry_platform
from renderer.renderer_lib.targets import render_targets, renders

# traefik-crds is the member only a vendored manifest admits.
MEMBER_FLOOR = {"pihole", "traefik", "flux", "traefik-crds"}


def _tree(tmp_path: Path, entries: str) -> tuple[Path, Path]:
    host_vars = tmp_path / "host_vars"
    host_vars.mkdir()
    (host_vars / "box.yml").write_text(f"containers_list:\n{entries}")
    return host_vars, tmp_path / "roles"


def _file(roles: Path, rel: str) -> None:
    path = roles / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\n")


def _old_set(host_vars: Path, roles: Path) -> set[str]:
    return {
        entry["name"]
        for entry in containers_entries(host_vars / "box.yml")
        if entry_platform(entry) == "k8s" and includes_manifests(roles / entry["name"])
    }


def test_the_old_and_new_predicates_agree_on_the_tree():
    old = {
        entry["name"]
        for entry in containers_entries(HOST_VARS / f"{K8S_HOST}.yml")
        if entry_platform(entry) == "k8s"
        and includes_manifests(K8S_ROLES / entry["name"])
    }
    assert set(render_targets(K8S_HOST)) == old


def test_a_role_with_nothing_to_render_is_left_out_of_both(tmp_path):
    host_vars, roles = _tree(tmp_path, "  - name: empty\n    platform: k8s\n")
    _file(roles, "empty/defaults/main.yml")
    assert render_targets("box", host_vars, roles) == []
    assert _old_set(host_vars, roles) == set()


@pytest.mark.parametrize(
    ("entry", "rel"),
    [
        ({}, "templates/deployment.yaml.j2"),
        ({"port": 80}, "defaults/main.yml"),
        ({"hostname": "demo"}, "defaults/main.yml"),
        ({}, "files/upstream/crds.yml"),
    ],
)
def test_a_template_a_shared_default_or_a_vendored_file_renders(tmp_path, entry, rel):
    roles = tmp_path / "roles"
    _file(roles, f"demo/{rel}")
    assert renders({"name": "demo", **entry}, roles)


def test_a_config_template_alone_does_not_render(tmp_path):
    roles = tmp_path / "roles"
    _file(roles, "demo/templates/config/app.yaml.j2")
    assert not renders({"name": "demo"}, roles)


def test_the_set_holds_its_named_members():
    assert MEMBER_FLOOR - set(render_targets(K8S_HOST)) == set()


def test_a_set_without_traefik_fails_the_floor(tmp_path):
    entries = "".join(
        f"  - name: {name}\n    platform: k8s\n" for name in sorted(MEMBER_FLOOR)
    )
    host_vars, roles = _tree(tmp_path, entries)
    for name in MEMBER_FLOOR - {"traefik"}:
        _file(roles, f"{name}/templates/deployment.yaml.j2")
    assert MEMBER_FLOOR - set(render_targets("box", host_vars, roles)) == {"traefik"}


def test_a_host_with_no_vars_file_renders_nothing(tmp_path):
    assert render_targets("no-such-host", tmp_path) == []
