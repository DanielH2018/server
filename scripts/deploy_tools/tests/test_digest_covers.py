"""The case table for `digest_covers.py`: which ranges a render digest covers for a role (#4781).

Each row is a change, the role whose `k8s_unapplied` line it is judged for, and whether the
render proof may discharge that line. The rule was specified as this table rather than as
prose, because a rule stated only in prose kept drawing new bypasses in review. A new bypass
is a new must-keep row.

The tree is a throwaway git repo shaped like the real one where the rule reads it: a play with
a preamble, the toposort plugin's three filters, a role that acts outside its digest
(`authelia`'s command task reading `auth_key`), a role that does not (`sonarr`), and the shared
`manifests` role.

Run: uv run pytest scripts/deploy_tools/tests/test_digest_covers.py
"""

import json
import pytest

import digest_covers
from deployer_lib import deploy_narrow
from lib.git_testing import git_out

from _narrow_fixtures import Tree

HOST_VARS = """\
containers_list:
  - name: authelia
    platform: k8s
  - name: sonarr
    platform: k8s
"""

GROUP_VARS = "auth_key: a\nsonarr_key: s\n"

DEPLOY = """\
- hosts: all
  pre_tasks:
    - name: Load secrets
      ansible.builtin.import_tasks: pre_tasks/load_secrets.yml
  tasks:
    - name: Deploy each role
      ansible.builtin.include_role:
        name: "k8s/{{ item.name }}"
      loop: "{{ containers_list | toposort_containers(containers_list | build_k8s_dep_map('.')) }}"
"""

LOAD_SECRETS = """\
- name: Load the secrets
  ansible.builtin.set_fact:
    loaded: true
"""

TOPOSORT = """\
def _edges(c):
    return c.get("depends_on", [])


def _platform_of(c):
    return c.get("platform", "docker")


def toposort_containers(containers_list, deps_map):
    return list(containers_list)


def build_k8s_dep_map(containers_list, playbook_dir):
    return {c["name"]: _edges(c) for c in containers_list}


def filter_by_platform(containers_list, platform="docker"):
    return [c for c in containers_list if _platform_of(c) == platform]


class FilterModule:
    def filters(self):
        return {
            "build_k8s_dep_map": build_k8s_dep_map,
            "toposort_containers": toposort_containers,
            "filter_by_platform": filter_by_platform,
        }
"""

AUTHELIA_TASKS = """\
# Hash the users database before the render.
- name: Hash the users database
  ansible.builtin.command: "files/hash.sh {{ auth_key }}"
  when: not (skip_hash | default(false))
- name: Render and apply
  ansible.builtin.include_role:
    name: k8s/manifests
"""

RELEASE_STAMP = """\
# Every self-rollouts role gates on the stamp's three facts.
- name: Write the release record
  ansible.builtin.copy:
    content: "{{ manifests_release | to_json }}"
    dest: /var/lib/homelab/k8s-releases.d/x.json
"""

BASE = {
    "ansible.cfg": "[defaults]\nforks = 5\n",
    "ansible/inventory/hosts.ini": "[all]\ndaniel-box\n",
    "ansible/inventory/host_vars/daniel-box.yml": HOST_VARS,
    "ansible/inventory/group_vars/all.yml": GROUP_VARS,
    "ansible/deploy.yml": DEPLOY,
    "ansible/pre_tasks/load_secrets.yml": LOAD_SECRETS,
    "ansible/filter_plugins/toposort.py": TOPOSORT,
    "ansible/roles/k8s/authelia/tasks/main.yml": AUTHELIA_TASKS,
    "ansible/roles/k8s/authelia/files/hash.sh": '#!/bin/sh\nhash "$1"\n',
    "ansible/roles/k8s/sonarr/tasks/main.yml": (
        "- ansible.builtin.include_role:\n    name: k8s/manifests\n"
    ),
    "ansible/roles/k8s/sonarr/templates/deployment.yaml.j2": "k: {{ sonarr_key }}\n",
    "ansible/roles/k8s/manifests/tasks/main.yml": (
        "- ansible.builtin.import_tasks: release_stamp.yml\n"
    ),
    "ansible/roles/k8s/manifests/tasks/release_stamp.yml": RELEASE_STAMP,
}

# The #4753 shape: a play var read only by a new preamble assert.
STUB_ASSERT = """\
- name: Refuse a stub file outside a dry run
  ansible.builtin.assert:
    that: not stubbed or (stubs_allowed | bool)
  vars:
    stubbed: "{{ homelab_secret_stubs | default(false) }}"
"""


def _play_vars(line: str) -> str:
    """`DEPLOY` with a play-level `vars:` holding `line`."""
    return DEPLOY.replace("- hosts: all\n", f"- hosts: all\n  vars:\n    {line}\n")


DEPLOY_WITH_PLAY_VAR = _play_vars("stubs_allowed: true")


def _play_pre_task(body: str) -> str:
    """`DEPLOY` with one more task in the play's own `pre_tasks:`, ahead of the preamble."""
    return DEPLOY.replace(
        "  pre_tasks:\n", f"  pre_tasks:\n    - name: Added\n      {body}\n"
    )


# The #4836 shape: a new constant only `build_k8s_dep_map` reads.
# #4836's own body: locals built, updated and assigned, and no entry written.
TOPOSORT_LOCAL_WRITES = TOPOSORT.replace(
    "def toposort_containers", "LAST = {'netpol-baseline'}\n\n\ndef toposort_containers"
).replace(
    '    return {c["name"]: _edges(c) for c in containers_list}\n',
    """    dep_map = {}
    names = [c["name"] for c in containers_list]
    for c in containers_list:
        deps = set(_edges(c))
        if c["name"] in LAST:
            deps.update(n for n in names if n not in LAST)
        dep_map[c["name"]] = sorted(deps)
    return dep_map
""",
)


def _dep_map_body(body: str) -> str:
    """`TOPOSORT` with `build_k8s_dep_map` running `body` before its return."""
    return TOPOSORT.replace(
        '    return {c["name"]: _edges(c) for c in containers_list}\n',
        body + '    return {c["name"]: _edges(c) for c in containers_list}\n',
    )


TOPOSORT_SORTS_LAST = TOPOSORT.replace(
    "def toposort_containers", "LAST = {'netpol-baseline'}\n\n\ndef toposort_containers"
).replace("_edges(c) for c", "_edges(c) or list(LAST) for c")


def _edit(path: str, old: str, new: str) -> dict[str, str]:
    assert old in BASE[path], f"{old!r} is not in {path}"
    return {path: BASE[path].replace(old, new)}


ROWS = [
    # ── must discharge ──────────────────────────────────────────────────────────────────────
    pytest.param(
        "authelia",
        [{"ansible/deploy.yml": DEPLOY_WITH_PLAY_VAR}]
        + [{"ansible/pre_tasks/load_secrets.yml": LOAD_SECRETS + STUB_ASSERT}],
        None,
        id="4753-assert-and-its-play-var",
    ),
    pytest.param(
        "authelia",
        [
            _edit(
                "ansible/filter_plugins/toposort.py",
                "_edges(c) for c",
                "list(_edges(c)) for c",
            )
        ],
        None,
        id="4836-order-only-toposort",
    ),
    pytest.param(
        "authelia",
        [{"ansible/filter_plugins/toposort.py": TOPOSORT_SORTS_LAST}],
        None,
        id="4836-order-only-helper-the-other-filter-never-reaches",
    ),
    pytest.param(
        "manifests",
        [_edit("ansible/roles/k8s/manifests/tasks/release_stamp.yml", "Every", "Each")],
        None,
        id="4818-comment-in-a-shared-roles-tasks",
    ),
    pytest.param(
        "authelia",
        [_edit("ansible/roles/k8s/authelia/tasks/main.yml", "# Hash", "# Rehash")],
        None,
        id="comment-in-the-roles-own-tasks",
    ),
    pytest.param(
        "authelia",
        [
            _edit(
                "ansible/inventory/group_vars/all.yml", "sonarr_key: s", "sonarr_key: t"
            )
        ],
        None,
        id="inventory-key-only-another-role-reads",
    ),
    pytest.param(
        "authelia",
        [_edit("ansible/roles/k8s/sonarr/templates/deployment.yaml.j2", "k:", "key:")],
        None,
        id="another-roles-own-change",
    ),
    pytest.param(
        "authelia",
        [
            {
                "ansible/deploy.yml": _play_pre_task(
                    "ansible.builtin.assert:\n        that: true"
                )
            }
        ],
        None,
        id="an-assert-in-the-plays-own-pre-tasks",
    ),
    pytest.param(
        "authelia",
        [
            {
                "ansible/templates/host-aliases.yml.j2": "{% macro aliases() %}{% endmacro %}\n",
                "ansible/roles/k8s/sonarr/templates/deployment.yaml.j2": (
                    "{% from 'host-aliases.yml.j2' import aliases %}\n"
                    + BASE["ansible/roles/k8s/sonarr/templates/deployment.yaml.j2"]
                ),
            }
        ],
        None,
        id="a-new-shared-template-only-another-role-imports",
    ),
    pytest.param(
        "authelia",
        [{"ansible/filter_plugins/toposort.py": TOPOSORT_LOCAL_WRITES}],
        None,
        id="4836-order-only-body-that-writes-only-its-own-locals",
    ),
    pytest.param(
        "authelia",
        [
            {
                "ansible/filter_plugins/toposort.py": _dep_map_body(
                    "    global _SEEN\n    _SEEN = containers_list\n"
                )
            }
        ],
        None,
        id="build-k8s-dep-map-binds-a-global-and-writes-no-entry",
    ),
    # ── must keep ───────────────────────────────────────────────────────────────────────────
    pytest.param(
        "authelia",
        [_edit("ansible/roles/k8s/authelia/tasks/main.yml", "hash.sh", "hash2.sh")],
        "tasks/main.yml changes authelia's own role",
        id="a-task-of-the-roles-own-changes",
    ),
    pytest.param(
        "authelia",
        [_edit("ansible/roles/k8s/authelia/files/hash.sh", "hash ", "rehash ")],
        "files/hash.sh changes authelia's own role",
        id="a-file-the-roles-own-task-reads",
    ),
    pytest.param(
        "manifests",
        [
            _edit(
                "ansible/roles/k8s/manifests/tasks/release_stamp.yml",
                "x.json",
                "y.json",
            )
        ],
        "release_stamp.yml changes manifests's own role",
        id="a-shared-roles-task-changes",
    ),
    pytest.param(
        "authelia",
        [
            _edit(
                "ansible/pre_tasks/load_secrets.yml",
                "loaded: true",
                "loaded: yes-please",
            )
        ],
        "not an assert, fail or debug",
        id="a-set-fact-in-the-preamble-changes",
    ),
    pytest.param(
        "authelia",
        [
            {
                "ansible/deploy.yml": _play_pre_task(
                    "ansible.builtin.set_fact:\n        skip_hash: true"
                )
            }
        ],
        "ansible/deploy.yml changes a task that is not an assert, fail or debug",
        id="a-set-fact-in-the-plays-own-pre-tasks",
    ),
    pytest.param(
        "authelia",
        [{"ansible/deploy.yml": _play_vars("skip_hash: true")}],
        "the variable skip_hash, which ansible/roles/k8s/authelia/tasks/main.yml reads",
        id="a-play-var-the-roles-own-task-reads",
    ),
    pytest.param(
        "authelia",
        [{"ansible/deploy.yml": _play_vars("auth_key: z")}],
        "changes the variable auth_key, which ansible/inventory/group_vars/all.yml reads",
        id="a-play-var-shadowing-an-inventory-key",
    ),
    pytest.param(
        "authelia",
        [
            {
                "ansible/pre_tasks/load_secrets.yml": LOAD_SECRETS
                + "- ansible.builtin.assert:\n    that: true\n  register: checked\n"
            }
        ],
        "not an assert, fail or debug",
        id="an-assert-whose-result-is-registered",
    ),
    pytest.param(
        "authelia",
        [{"ansible/deploy.yml": _play_vars("ansible_become_user: nobody")}],
        "changes the variable ansible_become_user, which Ansible itself reads",
        id="a-play-var-ansible-reads-with-no-file-naming-it",
    ),
    pytest.param(
        "authelia",
        [
            _edit(
                "ansible/filter_plugins/toposort.py",
                "    return list(containers_list)\n",
                "    return [dict(c, use_authelia=False) for c in containers_list]\n",
            )
        ],
        "every deploy runs",
        id="toposort-containers-changes-what-it-returns",
    ),
    pytest.param(
        "authelia",
        [
            {
                "ansible/filter_plugins/toposort.py": _dep_map_body(
                    "    for c in containers_list:\n"
                    "        c.setdefault('depends_on', []).append('x')\n"
                )
            }
        ],
        "every deploy runs",
        id="build-k8s-dep-map-writes-through-an-entry",
    ),
    pytest.param(
        "authelia",
        [
            {
                "ansible/filter_plugins/toposort.py": _dep_map_body(
                    "    for c in containers_list:\n        c['seen'] = True\n"
                )
            }
        ],
        "every deploy runs",
        id="build-k8s-dep-map-assigns-into-an-entry",
    ),
    pytest.param(
        "authelia",
        [
            {
                **_edit(
                    "ansible/filter_plugins/toposort.py", '"docker")\n', '"k8s")\n'
                ),
                # An entry with no `platform`, which the new default moves into the k8s play.
                "ansible/inventory/host_vars/daniel-box.yml": HOST_VARS
                + "  - name: x\n",
            }
        ],
        "every deploy runs",
        id="filter-by-platform-changes",
    ),
    pytest.param(
        "authelia",
        [
            _edit(
                "ansible/filter_plugins/toposort.py",
                'return c.get("platform',
                'return c.get("kind',
            )
        ],
        "every deploy runs",
        id="a-helper-filter-by-platform-reaches-changes",
    ),
    pytest.param(
        "authelia",
        [_edit("ansible/inventory/group_vars/all.yml", "auth_key: a", "auth_key: b")],
        "group_vars/all.yml reaches authelia",
        id="inventory-key-the-roles-own-task-reads",
    ),
    pytest.param(
        "authelia",
        [_edit("ansible.cfg", "forks = 5", "forks = 10")],
        "ansible.cfg is read by every deploy",
        id="a-path-no-rule-reads",
    ),
    pytest.param(
        "authelia",
        [
            _edit("ansible/roles/k8s/authelia/tasks/main.yml", "hash.sh", "hash2.sh"),
            {"ansible/pre_tasks/load_secrets.yml": LOAD_SECRETS + STUB_ASSERT},
        ],
        "tasks/main.yml changes authelia's own role",
        id="a-real-change-laundered-by-a-later-assert",
    ),
]


@pytest.fixture
def tree(tmp_path) -> Tree:
    t = Tree(tmp_path / "repo")
    for path, text in BASE.items():
        t.write(path, text)
    t.commit("base")
    return t


def _commit(tree: Tree, commits: list[dict[str, str]]) -> str:
    """Commit each of `commits` in turn, and return the last one."""
    for n, files in enumerate(commits):
        for path, text in files.items():
            tree.write(path, text)
        head = tree.commit(f"change {n}")
    return head


@pytest.mark.parametrize("role, commits, kept_because", ROWS)
def test_the_case_table(tree, role, commits, kept_because):
    """A must-keep row names the reason, so it cannot pass on an incidental one."""
    base = git_out(tree.root, "rev-parse", "HEAD")
    head = _commit(tree, commits)
    reason = digest_covers.Classifier(tree.root).reason(role, base, head)
    if kept_because is None:
        assert reason is None
    else:
        assert reason and kept_because in reason


def test_a_base_that_is_not_an_ancestor_keeps_the_line(tree):
    base = git_out(tree.root, "rev-parse", "HEAD")
    tree.write("ansible/pre_tasks/load_secrets.yml", LOAD_SECRETS + STUB_ASSERT)
    head = tree.commit("assert")
    git_out(tree.root, "checkout", "-q", "-b", "side", base)
    tree.write("README", "side\n")
    side = tree.commit("side")
    reason = digest_covers.Classifier(tree.root).reason("authelia", side, head)
    assert reason and "not an ancestor" in reason


def test_the_cli_prints_a_verdict_per_item_in_the_deployers_argv(tree, capsys):
    """The deployer's own argv drives the CLI, so the two cannot drift apart."""
    base = git_out(tree.root, "rev-parse", "HEAD")
    head = _commit(
        tree, [{"ansible/pre_tasks/load_secrets.yml": LOAD_SECRETS + STUB_ASSERT}]
    )
    item, kept = f"authelia:{base}:{head}", f"authelia:{'0' * 40}:{head}"
    argv = deploy_narrow.digest_covers_argv({item, kept})
    assert argv[4].endswith("digest_covers.py")
    assert digest_covers.main([*argv[5:], "--repo", str(tree.root)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {item: True, kept: False}
