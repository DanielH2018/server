"""The deployer's table of cross-role setup files matches what the setup roles' tasks ship.

`deploy_cross_role.SETUP_FILES_SHIPPED_BY_OTHER_ROLES` maps a file under one setup role's `files/`
or `tasks/` to the other setup roles that install or import it by path (#3306, #3317). The
GitOps deployer reads the table to re-apply those roles when the file changes, and it cannot
read the tree to find them itself: every caller of `setup_tags_for` passes paths alone. So the
table is static, and this test is what keeps it equal to the tree. A role that starts shipping
another role's file without a table entry would keep the old file on its host behind a green
apply.

The scan reads every `roles/setup/<owner>/<files|tasks>/<f>` and
`{{ role_path }}/../<owner>/<files|tasks>/<f>` reference in a setup role's `tasks/`, which
catches a copy task's `src:`, a `stamp_deployed_pairs` entry and an `import_tasks:` alike. A
task file needs its importers re-applied for the same reason a shipped file does: the import
is static, so the imported tasks run under each importer's own tags and nowhere else.

`common` is an owner like any other (#3312). Its `host_lib.py` and task files reach a host
only through the roles that copy or import them, so a change re-applies those roles, and
`setup_roles_for` drops `common` itself from the result because no playbook applies it on its
own. The scan sees a `host_lib.py` consumer through its `stamp_deployed_pairs` entry, which
`test_host_lib_sibling_copies.py` requires beside every `install_host_lib.yml` import.

A `templates/` file a shared task file renders inherits that task file's importers (#3319): the
kuma-check pair reaches a host only through `kuma_check_timer.yml`, which names it under its own
role, so the direct scan alone finds no consumer. `SETUP_FILES_ROUTED_TO_OWNER` is the one
exclusion, and its `DECIDED:` marker in `deploy_cross_role` says why `resolv.conf.j2` records
`common` instead.

The k8s roles importing a setup file are a second table, `K8S_ROLES_IMPORTING_SETUP_FILES`
(#3320). Their import spelling climbs one more level (`{{ role_path }}/../../setup/<owner>/`),
and the deployer defer-and-alerts them rather than re-applying them.

A file outside `ansible/roles/k8s/` that a k8s role's image embeds is a third table,
`K8S_BUILD_CONTEXT_FILES` (#4840). The scan reads each `image_builder_context_files` and
`image_builder_context` key in a k8s role's tasks, resolves `{{ playbook_dir }}` to `ansible`,
and keeps a source outside the role itself.

The setup roles calling a filter plugin's filters, `SETUP_ROLES_CALLING_FILTER_PLUGINS`, and
the setup roles reading another's `defaults/` variable have their own census in
`test_setup_cross_role_reads.py`.

Run: uv run pytest ansible/tests/setup/test_setup_cross_role_files.py
"""

import fnmatch
import posixpath
import re

from _helpers import ROLES
from _role_census import role_dirs, role_task_files

from lib import yaml_fast

from deployer_lib.deploy_cross_role import (
    K8S_BUILD_CONTEXT_FILES,
    K8S_ROLES_IMPORTING_SETUP_FILES,
    SETUP_FILES_ROUTED_TO_OWNER,
    SETUP_FILES_SHIPPED_BY_OTHER_ROLES,
)

_REFERENCE = re.compile(
    r"(?:roles/setup/|\{\{ role_path \}\}/\.\./)([a-z0-9_]+)/(files|tasks|templates)/([\w./-]*\w)"
)
# A k8s role reaches a setup role's file one directory further up. `{{ role_path }}/../<x>/`
# from a k8s role names a k8s sibling, so the setup-only `_REFERENCE` alternative must not match.
_K8S_REFERENCE = re.compile(
    r"(?:roles/setup/|\{\{ role_path \}\}/\.\./\.\./setup/)([a-z0-9_]+)/(files|tasks|templates)/([\w./-]*\w)"
)
# Named members, so a scan that stops matching fails by name rather than agreeing with an
# empty table.
KNOWN_EDGES = {
    "ansible/roles/setup/common/tasks/install_host_lib.yml": {
        "fake_remux",
        "gitops_deploy",
        "k3s",
        "renovate_agent",
        "renovate_notify",
    },
    "hosts/lib/host_lib.py": {
        "fake_remux",
        "gitops_deploy",
        "k3s",
        "renovate_agent",
        "renovate_notify",
    },
    "ansible/roles/setup/gitops_deploy/files/gitops_markers.py": {
        "deploy_ui",
        "renovate_agent",
    },
    # Reached only through kuma_check_timer.yml, so this is the transitive edge.
    "ansible/roles/setup/common/templates/kuma-check.timer.j2": {
        "gitops_deploy",
        "initial_setup",
        "k3s",
        "render_records",
    },
}
KNOWN_K8S_EDGES = {
    "hosts/lib/host_lib.py": {"configarr", "janitorr"},
    "hosts/lib/kuma_push.py": {
        "autofix-bridge",
        "configarr",
        "crowdsec",
        "janitorr",
        "uptime-kuma",
    },
}

KNOWN_BUILD_CONTEXT_EDGES = {
    "scripts/diagnostics/probe_lib/obs_api.py": {"homelab-mcp"},
}
_CONTEXT_VARS = ("image_builder_context", "image_builder_context_files")


def _context_sources(node):
    """Every key of an `image_builder_context*` mapping anywhere inside parsed task YAML."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _CONTEXT_VARS and isinstance(value, dict):
                yield from value
            else:
                yield from _context_sources(value)
    elif isinstance(node, list):
        for item in node:
            yield from _context_sources(item)


def build_context_files(task_docs: dict[str, object]) -> dict[str, frozenset[str]]:
    """Each repo path a k8s role's image build context names outside that role, mapped to the roles."""
    edges: dict[str, set[str]] = {}
    for role, doc in task_docs.items():
        for src in _context_sources(doc):
            path = posixpath.normpath(src.replace("{{ playbook_dir }}", "ansible"))
            if not path.startswith(f"ansible/roles/k8s/{role}/"):
                edges.setdefault(path, set()).add(role)
    return {path: frozenset(roles) for path, roles in edges.items()}


def _k8s_task_docs() -> dict[str, object]:
    return {
        role_dir.name: [
            yaml_fast.safe_load(p.read_text()) for p in role_task_files(role_dir)
        ]
        for role_dir in role_dirs(ROLES / "k8s")
    }


# A module the setup roles ship that sits outside the setup tree since H7.2e: the shared
# `hosts/lib/` modules and the `scripts/lib/longhorn_lib/` package. No role owns them, so every
# role naming one is a shipper. The lookbehind keeps `old_hosts/lib/x.py`-shaped words out.
_SHARED_FILE = re.compile(
    r"(?<![\w.])((?:hosts/lib/|scripts/lib/longhorn_lib/)[\w-]+\.py)"
)


def cross_role_files(
    task_texts: dict[str, str], pattern: re.Pattern = _REFERENCE
) -> dict[str, frozenset[str]]:
    """Each path a role's task text ships from outside itself, mapped to those roles.

    That is a setup-role path named under ANOTHER role, and a shared module under `hosts/lib/`
    or `scripts/lib/longhorn_lib/`, which no role owns.
    """
    edges: dict[str, set[str]] = {}
    for role, text in task_texts.items():
        for owner, kind, rel in pattern.findall(text):
            if owner == role:
                continue
            edges.setdefault(f"ansible/roles/setup/{owner}/{kind}/{rel}", set()).add(
                role
            )
        for shared in _SHARED_FILE.findall(text):
            edges.setdefault(shared, set()).add(role)
    return {path: frozenset(roles) for path, roles in edges.items()}


# A template path a task file names, with any `{{ ... }}` left in it for `fnmatch` to expand.
_TEMPLATE_REF = re.compile(
    r"roles/setup/([a-z0-9_]+)/templates/((?:\{\{.*?\}\}|[^\s\"'{])+)"
)


def with_rendered_templates(
    edges: dict[str, frozenset[str]], read, templates_of
) -> dict[str, frozenset[str]]:
    """`edges` plus each `templates/` file an imported task file renders from its own role.

    The template inherits the task file's importers, because it reaches a host only when one
    of them runs the import. `kuma_check_timer.yml` names its pair as
    `kuma-check.{{ item }}.j2`, so a Jinja expression in the path matches any template name.

    Args:
        edges: the direct edges `cross_role_files` found.
        read: a repo-relative task file path -> its text.
        templates_of: a role name -> the file names under its `templates/`.
    """
    out = {path: set(roles) for path, roles in edges.items()}
    for path, roles in edges.items():
        if "/tasks/" not in path:
            continue
        owner = path.split("/")[3]
        for ref_owner, rel in _TEMPLATE_REF.findall(read(path)):
            if ref_owner != owner:
                continue
            glob = re.sub(r"\{\{.*?\}\}", "*", rel)
            for name in fnmatch.filter(templates_of(owner), glob):
                out.setdefault(
                    f"ansible/roles/setup/{owner}/templates/{name}", set()
                ).update(roles)
    return {path: frozenset(roles) for path, roles in out.items()}


def _task_texts(plane: str) -> dict[str, str]:
    texts = {}
    for role_dir in role_dirs(ROLES / plane):
        tasks = role_task_files(role_dir)
        texts[role_dir.name] = "\n".join(p.read_text() for p in tasks)
    return texts


def _k8s_task_texts() -> dict[str, str]:
    # Comment lines dropped: k8s roles cite setup files in prose (game-stats explains why it
    # does NOT use host_lib.py), and no k8s comment ships a file.
    texts = {
        role: "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
        for role, text in _task_texts("k8s").items()
    }
    # A k8s role also ships a setup file from a ship list in defaults/ or a `lookup('file')` in
    # a template, as autofix-bridge and uptime-kuma stage kuma_push.py (#3745). Only lines
    # naming `playbook_dir` count: template prose cites setup paths too, and ships nothing.
    for role_dir in role_dirs(ROLES / "k8s"):
        shipped = [
            line
            for sub in ("defaults", "templates")
            for path in sorted((role_dir / sub).rglob("*"))
            if path.is_file()
            for line in path.read_text().splitlines()
            if "playbook_dir" in line and not line.lstrip().startswith("#")
        ]
        if shipped:
            texts[role_dir.name] = "\n".join([texts.get(role_dir.name, ""), *shipped])
    return texts


def _templates_of(role: str) -> list[str]:
    return [p.name for p in (ROLES / "setup" / role / "templates").glob("*")]


def _tree_edges() -> dict[str, frozenset[str]]:
    direct = cross_role_files(_setup_task_texts())
    found = with_rendered_templates(
        direct, lambda p: (ROLES.parent.parent / p).read_text(), _templates_of
    )
    return {p: r for p, r in found.items() if p not in SETUP_FILES_ROUTED_TO_OWNER}


def _setup_task_texts() -> dict[str, str]:
    return _task_texts("setup")


def test_the_deployers_table_equals_the_trees_cross_role_files():
    # The `defaults/` rows are a variable read, not a path, so their own census holds them.
    shipped = {
        path: roles
        for path, roles in SETUP_FILES_SHIPPED_BY_OTHER_ROLES.items()
        if "/defaults/" not in path
    }
    assert _tree_edges() == shipped


def test_the_scan_finds_the_known_edges():
    found = _tree_edges()
    for path, roles in KNOWN_EDGES.items():
        assert found.get(path) == roles, path


def test_the_owner_routed_template_names_the_roles_that_render_it():
    # The exclusion is a decision about a real edge; one that no role names is dead weight.
    # The consumers are what `common`'s remediation prints (#4316), so a third renderer the
    # table does not name would go missing from that command.
    direct = cross_role_files(_setup_task_texts())
    for path, roles in SETUP_FILES_ROUTED_TO_OWNER.items():
        assert direct.get(path) == roles, path


def test_the_k8s_importers_table_equals_the_tree():
    found = cross_role_files(_k8s_task_texts(), _K8S_REFERENCE)
    assert found == K8S_ROLES_IMPORTING_SETUP_FILES
    for path, roles in KNOWN_K8S_EDGES.items():
        assert found.get(path) == roles, path


def test_the_build_context_table_equals_the_tree():
    found = build_context_files(_k8s_task_docs())
    assert found == K8S_BUILD_CONTEXT_FILES
    for path, roles in KNOWN_BUILD_CONTEXT_EDGES.items():
        assert found.get(path) == roles, path


def test_a_build_context_file_outside_the_role_is_flagged():
    task = [
        {
            "include_role": {"name": "k8s/image-builder"},
            "vars": {
                "image_builder_context_files": {
                    "{{ playbook_dir }}/roles/k8s/svc/files/app.py": "app.py",
                    "{{ playbook_dir }}/../scripts/lib/shared.py": "shared.py",
                },
                "image_builder_context": {
                    "{{ playbook_dir }}/roles/k8s/other/templates/x.j2": "x",
                },
            },
        }
    ]
    assert build_context_files({"svc": task}) == {
        "scripts/lib/shared.py": frozenset({"svc"}),
        "ansible/roles/k8s/other/templates/x.j2": frozenset({"svc"}),
    }


def test_a_role_shipping_another_roles_file_is_flagged():
    task = 'src: "{{ playbook_dir }}/roles/setup/owner/files/shared.py"\n'
    loop = '- "{{ role_path }}/../owner/files/other.py"\n'
    assert cross_role_files({"owner": "", "consumer": task + loop}) == {
        "ansible/roles/setup/owner/files/shared.py": frozenset({"consumer"}),
        "ansible/roles/setup/owner/files/other.py": frozenset({"consumer"}),
    }


def test_a_role_naming_its_own_files_is_clean():
    own = "src: ansible/roles/setup/owner/files/shared.py\n"
    assert cross_role_files({"owner": own}) == {}


def test_a_role_naming_commons_file_is_flagged():
    common = "src: hosts/lib/host_lib.py\n"
    assert cross_role_files({"owner": common}) == {
        "hosts/lib/host_lib.py": frozenset({"owner"})
    }


def test_a_role_importing_commons_task_file_is_flagged():
    task = 'import_tasks: "{{ role_path }}/../common/tasks/shared.yml"\n'
    # A reference ending a sentence in a comment does not carry the full stop into the path.
    comment = "# see roles/setup/common/tasks/other.yml. Each script lands\n"
    assert cross_role_files({"owner": task + comment}) == {
        "ansible/roles/setup/common/tasks/shared.yml": frozenset({"owner"}),
        "ansible/roles/setup/common/tasks/other.yml": frozenset({"owner"}),
    }


def test_a_template_a_shared_task_file_renders_inherits_its_importers():
    edges = {"ansible/roles/setup/common/tasks/timer.yml": frozenset({"a", "b"})}
    task = 'src: "{{ playbook_dir }}/roles/setup/common/templates/unit.{{ item }}.j2"\n'
    names = ["unit.service.j2", "unit.timer.j2", "other.j2"]
    assert with_rendered_templates(edges, lambda _: task, lambda _: names) == {
        **edges,
        "ansible/roles/setup/common/templates/unit.service.j2": frozenset({"a", "b"}),
        "ansible/roles/setup/common/templates/unit.timer.j2": frozenset({"a", "b"}),
    }


def test_a_task_file_naming_no_template_adds_no_edge():
    edges = {"ansible/roles/setup/common/tasks/plain.yml": frozenset({"a"})}
    names = ["unit.j2"]
    assert (
        with_rendered_templates(edges, lambda _: "- debug: msg=hi\n", lambda _: names)
        == edges
    )


def test_a_k8s_role_importing_a_setup_task_file_is_flagged():
    task = 'import_tasks: "{{ role_path }}/../../setup/common/tasks/shared.yml"\n'
    # A k8s sibling one level up is not a setup role's file.
    sibling = 'import_tasks: "{{ role_path }}/../manifests/tasks/render.yml"\n'
    assert cross_role_files({"svc": task + sibling}, _K8S_REFERENCE) == {
        "ansible/roles/setup/common/tasks/shared.yml": frozenset({"svc"})
    }


def test_a_role_shipping_a_shared_module_from_outside_the_setup_tree_is_flagged():
    task = 'src: "{{ playbook_dir }}/../hosts/lib/ci_verdict.py"\n'
    loop = "- live: /opt/x/longhorn_backups.py\n  src: scripts/lib/longhorn_lib/longhorn_backups.py\n"
    assert cross_role_files({"consumer": task + loop}) == {
        "hosts/lib/ci_verdict.py": frozenset({"consumer"}),
        "scripts/lib/longhorn_lib/longhorn_backups.py": frozenset({"consumer"}),
    }


def test_a_path_that_only_ends_like_a_shared_module_is_clean():
    prose = "see old_hosts/lib/host_lib.py and hosts/lib/homelab-reconcile\n"
    assert cross_role_files({"consumer": prose}) == {}
