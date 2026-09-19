"""Every k8s role that carries a manifest template includes `k8s/manifests`.

`roles/k8s/manifests/` is the shared render -> apply -> queue contract every service role
includes (`ansible/roles/k8s/manifests/CLAUDE.md`); the release record, the rollout-restart
and the health gate all hang off it. A role that renders manifests and skips the include gets
none of that: `probe.py health` finds no release record and the central restart never fires
for it. Nothing reported the omission, and a role copied from the wrong sibling would land
green.

The population is derived, not listed: a role is in scope when `templates/` holds at least one
file `is_manifest_template` accepts, the same predicate the manifest validator renders by, so
`n8n-images` (two Dockerfiles, no manifest) drops out on the shape of its templates rather than
by name. The two `CALLER_RENDERED_ROLES` are exempt: `image-builder` and `volume-claim` render
for a CALLING role, inside its deploy, and apply what they render themselves — a release record
of their own would be the caller's under another name.

Run: uv run pytest ansible/tests/k8s/test_manifest_roles_include_the_shared_render.py
"""

from pathlib import Path

from _helpers import K8S_ROLES, load_tasks, walk_tasks


from lib.k8s_roles import CALLER_RENDERED_ROLES, is_manifest_template

SHARED_RENDER = "k8s/manifests"
_ROLE_KEYS = (
    "ansible.builtin.include_role",
    "include_role",
    "ansible.builtin.import_role",
    "import_role",
)

# Named members, so a predicate that stopped matching anything fails here rather than passing
# on an empty census.
KNOWN_MANIFEST_ROLES = frozenset({"sonarr", "traefik", "authelia", "configarr"})


def renders_manifests(role: Path) -> bool:
    templates = role / "templates"
    return templates.is_dir() and any(
        is_manifest_template(p) for p in templates.iterdir() if p.is_file()
    )


def includes_shared_render(role: Path) -> bool:
    for tasks_file in sorted((role / "tasks").glob("*.yml")):
        for task in walk_tasks(load_tasks(tasks_file)):
            for key in _ROLE_KEYS:
                spec = task.get(key)
                name = spec.get("name") if isinstance(spec, dict) else spec
                if name == SHARED_RENDER:
                    return True
    return False


def test_every_manifest_rendering_role_includes_the_shared_render():
    in_scope = {
        p.name
        for p in K8S_ROLES.iterdir()
        if p.is_dir() and p.name not in CALLER_RENDERED_ROLES and renders_manifests(p)
    }
    assert KNOWN_MANIFEST_ROLES <= in_scope
    missing = sorted(
        name for name in in_scope if not includes_shared_render(K8S_ROLES / name)
    )
    assert missing == [], (
        f"manifest templates but no `{SHARED_RENDER}` include: {missing}"
    )


def test_a_role_that_renders_and_skips_the_include_is_flagged(tmp_path: Path):
    """Red-proof pair, driven through the same two predicates the census uses."""
    role = tmp_path / "svc"
    (role / "templates").mkdir(parents=True)
    (role / "tasks").mkdir()
    (role / "templates" / "deployment.yaml.j2").write_text("kind: Deployment\n")
    (role / "tasks" / "main.yml").write_text(
        "- name: Apply\n  ansible.builtin.include_role:\n    name: k8s/cronjob-gate\n"
    )
    assert renders_manifests(role) and not includes_shared_render(role)
    (role / "tasks" / "main.yml").write_text(
        "- name: Apply\n  ansible.builtin.include_role:\n    name: k8s/manifests\n"
    )
    assert includes_shared_render(role)
    (role / "templates" / "deployment.yaml.j2").rename(
        role / "templates" / "Dockerfile.j2"
    )
    assert not renders_manifests(role)
