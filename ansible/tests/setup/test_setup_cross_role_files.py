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

The setup roles calling a filter plugin's filters are a third, `SETUP_ROLES_CALLING_FILTER_PLUGINS`
(#3874). `ansible/deploy.yml` runs no setup role, so the deployer re-applies or records them
beside the deploy plane. `_FILTER_CALLERS_EXEMPT` names each call that leaves nothing rendered
for a plugin change to stale, with the reason.

Run: uv run pytest ansible/tests/setup/test_setup_cross_role_files.py
"""

import fnmatch
import re

from _helpers import ROLES
from _role_census import role_dirs, role_task_files
from deploy_tools import narrow_filters

from deploy_cross_role import (
    K8S_ROLES_IMPORTING_SETUP_FILES,
    SETUP_FILES_ROUTED_TO_OWNER,
    SETUP_FILES_SHIPPED_BY_OTHER_ROLES,
    SETUP_ROLES_CALLING_FILTER_PLUGINS,
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
    "ansible/roles/setup/common/files/host_lib.py": {
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
# A setup role naming a plugin's filter where no rendered value can go stale, with the reason.
_FILTER_CALLERS_EXEMPT = {
    ("ansible/filter_plugins/toposort.py", "docker_install"): (
        "`filter_by_platform` runs in the `loop:` of `engine-upgrade.yml`, a never-tagged "
        "task file an operator runs by hand, so it renders nothing the plugin change leaves"
    ),
}
KNOWN_FILTER_CALLERS = {
    "ansible/filter_plugins/service_tier.py": {"k3s"},
    "ansible/filter_plugins/k8s_autodeploy.py": {"gitops_deploy"},
    "ansible/filter_plugins/longhorn_groups.py": {"k3s"},
}
_COMMENT_LINE = re.compile(r"^\s*#.*$", re.MULTILINE)
KNOWN_K8S_EDGES = {
    "ansible/roles/setup/common/files/host_lib.py": {"configarr", "janitorr"},
    "ansible/roles/setup/common/tasks/install_host_lib.yml": {"configarr", "janitorr"},
}


def cross_role_files(
    task_texts: dict[str, str], pattern: re.Pattern = _REFERENCE
) -> dict[str, frozenset[str]]:
    """Each setup-role path a role's task text names under ANOTHER role, mapped to those roles."""
    edges: dict[str, set[str]] = {}
    for role, text in task_texts.items():
        for owner, kind, rel in pattern.findall(text):
            if owner == role:
                continue
            edges.setdefault(f"ansible/roles/setup/{owner}/{kind}/{rel}", set()).add(
                role
            )
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
    return {
        role: "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
        for role, text in _task_texts("k8s").items()
    }


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
    assert _tree_edges() == SETUP_FILES_SHIPPED_BY_OTHER_ROLES


def test_the_scan_finds_the_known_edges():
    found = _tree_edges()
    for path, roles in KNOWN_EDGES.items():
        assert found.get(path) == roles, path


def test_the_owner_routed_template_is_still_rendered_by_another_role():
    # The exclusion is a decision about a real edge; one that no role names is dead weight.
    direct = cross_role_files(_setup_task_texts())
    for path in SETUP_FILES_ROUTED_TO_OWNER:
        assert direct.get(path), path


def test_the_k8s_importers_table_equals_the_tree():
    found = cross_role_files(_k8s_task_texts(), _K8S_REFERENCE)
    assert found == K8S_ROLES_IMPORTING_SETUP_FILES
    for path, roles in KNOWN_K8S_EDGES.items():
        assert found.get(path) == roles, path


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
    common = "src: ansible/roles/setup/common/files/host_lib.py\n"
    assert cross_role_files({"owner": common}) == {
        "ansible/roles/setup/common/files/host_lib.py": frozenset({"owner"})
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


def filter_callers(
    names: dict[str, set[str]], texts: dict[str, dict[str, str]]
) -> dict[str, frozenset[str]]:
    """Each plugin whose filter a setup role's YAML or template names, mapped to those roles.

    Args:
        names: a plugin path -> the filter names it registers.
        texts: a role -> {its file path: text}, `.py`, `.md` and `tests/` already left out:
            Python names a filter without piping into it, and neither of the other two
            reaches a host.
    """
    out: dict[str, set[str]] = {}
    for plugin, filters in names.items():
        words = re.compile(rf"(?<!\w)({'|'.join(map(re.escape, filters))})(?!\w)")
        for role, files in texts.items():
            if any(words.search(_COMMENT_LINE.sub("", t)) for t in files.values()):
                out.setdefault(plugin, set()).add(role)
    return {plugin: frozenset(roles) for plugin, roles in out.items()}


def _plugin_names() -> dict[str, set[str]]:
    repo = ROLES.parent.parent
    return {
        str(p.relative_to(repo)): narrow_filters.filter_names(p.read_text(), str(p))
        for p in sorted((ROLES.parent / "filter_plugins").glob("*.py"))
    }


def _setup_role_texts() -> dict[str, dict[str, str]]:
    texts = {}
    for role_dir in role_dirs(ROLES / "setup"):
        texts[role_dir.name] = {
            str(f): f.read_text()
            for f in sorted(role_dir.rglob("*"))
            if f.is_file()
            and f.suffix not in (".py", ".md", ".pyc")
            and "tests" not in f.relative_to(role_dir).parts
        }
    return texts


def _tree_filter_callers() -> dict[str, frozenset[str]]:
    found = filter_callers(_plugin_names(), _setup_role_texts())
    kept = {
        plugin: frozenset(r for r in roles if (plugin, r) not in _FILTER_CALLERS_EXEMPT)
        for plugin, roles in found.items()
    }
    return {plugin: roles for plugin, roles in kept.items() if roles}


def test_the_filter_callers_table_equals_the_tree():
    assert _tree_filter_callers() == SETUP_ROLES_CALLING_FILTER_PLUGINS
    for plugin, roles in KNOWN_FILTER_CALLERS.items():
        assert SETUP_ROLES_CALLING_FILTER_PLUGINS.get(plugin) == roles, plugin


def test_every_filter_caller_exemption_is_still_a_caller():
    # An exemption for a call that is gone is dead weight, and would hide a new one.
    found = filter_callers(_plugin_names(), _setup_role_texts())
    for plugin, role in _FILTER_CALLERS_EXEMPT:
        assert role in found.get(plugin, ()), (plugin, role)


def test_a_setup_role_calling_a_filter_is_flagged():
    texts = {"k3s": {"defaults/main.yml": "x: \"{{ list | tier_claims('a') }}\"\n"}}
    assert filter_callers({"p.py": {"tier_claims"}}, texts) == {
        "p.py": frozenset({"k3s"})
    }


def test_a_setup_role_naming_a_filter_only_in_a_comment_or_a_longer_word_is_clean():
    texts = {"k3s": {"a.yml": "# tier_claims is read here\ny: my_tier_claims_x\n"}}
    assert filter_callers({"p.py": {"tier_claims"}}, texts) == {}
