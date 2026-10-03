"""The deployer's table of cross-role setup files matches what the setup roles' tasks ship.

`deploy_changes.SETUP_FILES_SHIPPED_BY_OTHER_ROLES` maps a file under one setup role's `files/`
to the other setup roles that install it by path (#3306). The GitOps deployer reads the table to
re-apply those roles when the file changes, and it cannot read the tree to find them itself:
every caller of `setup_tags_for` passes paths alone. So the table is static, and this test is
what keeps it equal to the tree. A role that starts shipping another role's file without a
table entry would keep the old file on its host behind a green apply.

The scan reads every `roles/setup/<owner>/files/<f>` and `{{ role_path }}/../<owner>/files/<f>`
reference in a setup role's `tasks/`, which catches a copy task's `src:` and a
`stamp_deployed_pairs` entry alike.

`common` is an owner like any other (#3312). Its `host_lib.py` reaches a host only through
the roles that copy it, so a change re-applies those roles, and `setup_roles_for` drops
`common` itself from the result because no playbook applies it on its own. The scan sees a
consumer through its `stamp_deployed_pairs` entry, which `test_host_lib_sibling_copies.py`
requires beside every `install_host_lib.yml` import.

Run: uv run pytest ansible/tests/setup/test_setup_cross_role_files.py
"""

import re

from _helpers import ROLES
from _role_census import role_dirs

from deploy_changes import SETUP_FILES_SHIPPED_BY_OTHER_ROLES

_REFERENCE = re.compile(
    r"(?:roles/setup/|\{\{ role_path \}\}/\.\./)([a-z0-9_]+)/files/([\w./-]+)"
)
# Named members, so a scan that stops matching fails by name rather than agreeing with an
# empty table.
KNOWN_EDGES = {
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
}


def cross_role_files(task_texts: dict[str, str]) -> dict[str, frozenset[str]]:
    """Each `files/` path a role's task text names under ANOTHER role, mapped to those roles."""
    edges: dict[str, set[str]] = {}
    for role, text in task_texts.items():
        for owner, rel in _REFERENCE.findall(text):
            if owner == role:
                continue
            edges.setdefault(f"ansible/roles/setup/{owner}/files/{rel}", set()).add(
                role
            )
    return {path: frozenset(roles) for path, roles in edges.items()}


def _setup_task_texts() -> dict[str, str]:
    texts = {}
    for role_dir in role_dirs(ROLES / "setup"):
        tasks = sorted((role_dir / "tasks").rglob("*.yml"))
        texts[role_dir.name] = "\n".join(p.read_text() for p in tasks)
    return texts


def test_the_deployers_table_equals_the_trees_cross_role_files():
    assert cross_role_files(_setup_task_texts()) == SETUP_FILES_SHIPPED_BY_OTHER_ROLES


def test_the_scan_finds_the_known_edges():
    found = cross_role_files(_setup_task_texts())
    for path, roles in KNOWN_EDGES.items():
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
