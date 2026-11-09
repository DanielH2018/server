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

`common` is an owner like any other (#3312): a file of its reaches a host only through the
roles that copy or import it, and `setup_roles_for` drops `common` itself from the result
because no playbook applies it on its own. H7.63 deleted its last task files, so no live role
ships another role's file and the table holds only rows for roles that run on no host.

A `templates/` file a shared task file renders inherits that task file's importers (#3319),
because the direct scan alone finds no consumer for it. `SETUP_FILES_ROUTED_TO_OWNER` is the
one exclusion, and it is empty since H7.35.

A setup template that only a host component renders, through `role:`, and that no setup task
names is in `SETUP_FILES_RENDERED_ONLY_BY_HOST_PACKAGES`. The package applies it, so the
deployer re-applies no role and records no `manual_plane` line for it.

A repo file a setup task copies from the checkout by a literal `src: "{{ playbook_dir
}}/../<path>"` is a row too (#6411). It sits under no role, so without one the deployer maps
it to nothing. `checkout_files` finds it; a copy in a `common/tasks/` file maps to that file's
importers, since `common` is applied only through them.

The setup roles calling a filter plugin's filters, `SETUP_ROLES_CALLING_FILTER_PLUGINS`, and
the setup roles reading another's `defaults/` variable have their own census in
`test_setup_cross_role_reads.py`.

Run: uv run pytest ansible/tests/setup/test_setup_cross_role_files.py
"""

import fnmatch
import re

from _helpers import ROLES, SETUP_ROLES, load_yaml
from _role_census import role_dirs, role_task_files
from lib.role_installs import runs_on_no_host

from deployer_lib.deploy_cross_role import (
    SETUP_FILES_RENDERED_ONLY_BY_HOST_PACKAGES,
    SETUP_FILES_ROUTED_TO_OWNER,
    SETUP_FILES_SHIPPED_BY_OTHER_ROLES,
)

_REFERENCE = re.compile(
    r"(?:roles/setup/|\{\{ role_path \}\}/\.\./)([a-z0-9_]+)/(files|tasks|templates)/([\w./-]*\w)"
)
# No live setup role ships another's file since H7.63 deleted `common/tasks/`, so the table and
# the tree agree on nothing. The scan's non-vacuity is a live role whose tasks it must read, and
# the synthetic cases below prove each edge shape is found.
LIVE_ROLE = "k3s"
# A module the setup roles ship that sits outside the setup tree since H7.2e: the shared
# `hosts/lib/` modules and the `scripts/lib/longhorn_lib/` package. No role owns them, so every
# role naming one is a shipper. The lookbehind keeps `old_hosts/lib/x.py`-shaped words out.
_SHARED_FILE = re.compile(
    r"(?<![\w.])((?:hosts/lib/|scripts/lib/longhorn_lib/)[\w-]+\.py)"
)


def cross_role_files(task_texts: dict[str, str]) -> dict[str, frozenset[str]]:
    """Each path a role's task text ships from outside itself, mapped to those roles.

    That is a setup-role path named under ANOTHER role, and a shared module under `hosts/lib/`
    or `scripts/lib/longhorn_lib/`, which no role owns.
    """
    edges: dict[str, set[str]] = {}
    for role, text in task_texts.items():
        for owner, kind, rel in _REFERENCE.findall(text):
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
    of them runs the import. `check_timer.yml` names its pair as
    `check-timer.{{ item }}.j2`, so a Jinja expression in the path matches any template name.

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


def _templates_of(role: str) -> list[str]:
    return [p.name for p in (ROLES / "setup" / role / "templates").glob("*")]


def _tree_edges() -> dict[str, frozenset[str]]:
    direct = cross_role_files(_setup_task_texts())
    found = with_rendered_templates(
        direct, lambda p: (ROLES.parent.parent / p).read_text(), _templates_of
    )
    for path, roles in checkout_files(_setup_task_files(), direct).items():
        found[path] = found.get(path, frozenset()) | roles
    return {p: r for p, r in found.items() if p not in SETUP_FILES_ROUTED_TO_OWNER}


def _setup_task_texts() -> dict[str, str]:
    return _task_texts("setup")


# A task copying a repo file from the checkout by a literal path. A `{{ item }}` source names
# no file, and a comment line never starts with `src:`, so neither matches.
_CHECKOUT_SRC = re.compile(
    r'^\s*src:\s*"\{\{ playbook_dir \}\}/\.\./([\w./-]+)"\s*$', re.M
)


def checkout_files(
    task_files: dict[str, str], edges: dict[str, frozenset[str]]
) -> dict[str, frozenset[str]]:
    """Each repo file a setup task copies from the checkout, mapped to the roles that apply it.

    A copy in a role's own task file is that role's. A copy in a `common/tasks/` file reaches a
    host only through that file's importers, which `edges` holds (#6411).

    Args:
        task_files: a repo-relative task file path -> its text.
        edges: the cross-role edges `cross_role_files` found.
    """
    out: dict[str, set[str]] = {}
    for path, text in task_files.items():
        role = path.split("/")[3]
        appliers = edges.get(path, frozenset()) if role == "common" else {role}
        for src in _CHECKOUT_SRC.findall(text):
            out.setdefault(src, set()).update(appliers)
    return {path: frozenset(roles) for path, roles in out.items()}


def _setup_task_files() -> dict[str, str]:
    repo = ROLES.parent.parent
    return {
        p.relative_to(repo).as_posix(): p.read_text()
        for role_dir in role_dirs(ROLES / "setup")
        for p in role_task_files(role_dir)
    }


def without_roles_that_run_nowhere(
    table: dict[str, frozenset[str]], runs_nowhere
) -> dict[str, frozenset[str]]:
    """`table` minus the roles that run on no host, and minus any row left with no role.

    An emptied role applies to no host, so a row naming it re-applies nothing.

    Args:
        table: a file path -> the roles a change to it re-applies.
        runs_nowhere: a role name -> whether its tasks end the role on every host.
    """
    kept = {
        path: frozenset(r for r in roles if not runs_nowhere(r))
        for path, roles in table.items()
    }
    return {path: roles for path, roles in kept.items() if roles}


def test_the_deployers_table_equals_the_trees_cross_role_files():
    # The `defaults/` rows are a variable read, not a path, so their own census holds them.
    shipped = without_roles_that_run_nowhere(
        {
            path: roles
            for path, roles in SETUP_FILES_SHIPPED_BY_OTHER_ROLES.items()
            if "/defaults/" not in path
        },
        lambda role: runs_on_no_host(SETUP_ROLES / role),
    )
    # A file reached only through a task file nothing imports reaches no role.
    assert {path: roles for path, roles in _tree_edges().items() if roles} == shipped


def test_the_scan_reads_a_live_roles_tasks():
    assert not runs_on_no_host(SETUP_ROLES / LIVE_ROLE)
    assert _setup_task_texts().get(LIVE_ROLE), (
        f"the scan read no task text for {LIVE_ROLE}"
    )


def test_the_owner_routed_template_names_the_roles_that_render_it():
    # The exclusion is a decision about a real edge; one that no role names is dead weight.
    # The consumers are what `common`'s remediation prints (#4316), so a third renderer the
    # table does not name would go missing from that command.
    direct = cross_role_files(_setup_task_texts())
    for path, roles in SETUP_FILES_ROUTED_TO_OWNER.items():
        assert direct.get(path) == roles, path


def test_the_emptied_roles_run_on_no_host():
    assert runs_on_no_host(SETUP_ROLES / "gitops_deploy")
    assert runs_on_no_host(SETUP_ROLES / "initial_setup")
    assert not runs_on_no_host(SETUP_ROLES / LIVE_ROLE)


def test_a_row_naming_an_emptied_role_is_dropped_and_a_live_one_is_kept():
    table = {
        "a": frozenset({"emptied"}),
        "b": frozenset({"emptied", "live"}),
        "c": frozenset({"live"}),
    }
    assert without_roles_that_run_nowhere(table, lambda r: r == "emptied") == {
        "b": frozenset({"live"}),
        "c": frozenset({"live"}),
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


def test_a_role_copying_a_checkout_file_is_flagged():
    shared = "ansible/roles/setup/common/tasks/shared.yml"
    copy = '    src: "{{ playbook_dir }}/../scripts/x/tool.sh"\n'
    files = {
        "ansible/roles/setup/owner/tasks/main.yml": copy,
        shared: copy.replace("x/tool", "y/lib"),
    }
    assert checkout_files(files, {shared: frozenset({"importer"})}) == {
        "scripts/x/tool.sh": frozenset({"owner"}),
        "scripts/y/lib.sh": frozenset({"importer"}),
    }


def test_a_templated_or_commented_checkout_source_is_clean():
    text = (
        '    src: "{{ playbook_dir }}/../{{ item }}"\n'
        '  # src: "{{ playbook_dir }}/../scripts/x/tool.sh"\n'
        '    creates: "{{ playbook_dir }}/../.git/hooks/pre-commit"\n'
    )
    assert checkout_files({"ansible/roles/setup/owner/tasks/main.yml": text}, {}) == {}


def package_only_templates(specs: list[dict], task_texts: dict[str, str]) -> set[str]:
    """Each setup template a component renders with `role:` that no setup task names.

    Every list in a spec counts, so a `files:` entry and an `outputs:` one alike.

    Args:
        specs: the parsed `component.yml` files.
        task_texts: the setup roles' task text, by role.
    """
    named = "\n".join(task_texts.values())
    found = set()
    for spec in specs:
        role = spec.get("role")
        entries = [e for v in spec.values() if isinstance(v, list) for e in v]
        for entry in entries:
            src = str(entry.get("src", "")) if isinstance(entry, dict) else ""
            name = src.removeprefix("role:")
            if role and name != src and name.removesuffix(".j2") not in named:
                found.add(f"ansible/roles/setup/{role}/templates/{name}")
    return found


def _component_specs() -> list[dict]:
    components = ROLES.parent.parent / "hosts" / "components"
    return [load_yaml(p) or {} for p in sorted(components.glob("*/component.yml"))]


def test_the_package_only_table_equals_the_templates_only_components_render():
    found = package_only_templates(_component_specs(), _setup_task_texts())
    assert "ansible/roles/setup/common/templates/resolv.conf.j2" in found
    assert found == SETUP_FILES_RENDERED_ONLY_BY_HOST_PACKAGES


def test_a_role_template_only_a_component_renders_is_flagged():
    spec = {
        "role": "nut_host",
        "files": [{"src": "role:a.sh.j2"}],
        "outputs": [{"src": "role:b.conf.j2"}],
    }
    assert package_only_templates([spec], {"k3s": ""}) == {
        "ansible/roles/setup/nut_host/templates/a.sh.j2",
        "ansible/roles/setup/nut_host/templates/b.conf.j2",
    }


def test_a_role_template_a_setup_task_names_is_clean():
    spec = {
        "role": "k3s",
        "files": [{"src": "role:netplan.yaml.j2"}, {"src": "templates/own.j2"}],
    }
    task = 'src: "{{ item }}.j2"\nloop: [netplan.yaml]\n'
    assert package_only_templates([spec], {"k3s": task}) == set()
