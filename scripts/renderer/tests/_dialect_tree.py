"""The dialect's spec fixture for variable semantics: a role tree and its inventory.

`write_tree` writes them, and `TREE_LITERAL` is what the template renders to. The PR that added
them checked the literal once against an `ansible-playbook` render of the same tree.
"""

from pathlib import Path

from lib.estate import Inventory

_TREE_TEMPLATE = """\
default={{ only_default }}
group={{ group_over_default }}
host={{ host_over_group }}
include={{ manifests_deferred_dir_name }}
derived={{ derived }}
hostname={{ inventory_hostname }}
own_entry={{ hostvars['h1'].host_over_group }}
other_host_file={{ hostvars['h2'].host_file_over_inline }}
other_host_inline={{ hostvars['h2'].inline_only }}
other_host_resolved={{ hostvars['h2'].ansible_host }}
missing_with_default={{ absent | default('fallback') }}
"""
_ROLE_DEFAULTS = """\
only_default: from-defaults
group_over_default: from-defaults
host_over_group: from-defaults
manifests_deferred_dir_name: from-defaults
derived: "derived-{{ host_over_group }}"
"""
_GROUP_VARS = """\
group_over_default: from-group
host_over_group: from-group
manifests_deferred_dir_name: from-group
group_only: from-group
"""
_H1_VARS = """\
host_over_group: from-host
manifests_deferred_dir_name: from-host
containers_list:
  - name: widget
    platform: k8s
"""
_H2_VARS = """\
server_ip: 10.9.9.9
host_file_over_inline: from-host-file
"""
_HOSTS_INI = """\
[k8s]
h1 ansible_connection=local
h2 ansible_connection=local ansible_host={{server_ip}} inline_only=from-inline host_file_over_inline=from-inline
"""
_ROLE_TASKS = """\
- name: Render
  ansible.builtin.include_role:
    name: k8s/manifests
  vars:
    manifests_deferred_dir_name: from-include
    manifests_prune: false
"""

# What `_TREE_TEMPLATE` renders to on h1, with no extra vars.
TREE_LITERAL = """\
default=from-defaults
group=from-group
host=from-host
include=from-include
derived=derived-from-host
hostname=h1
own_entry=from-host
other_host_file=from-host-file
other_host_inline=from-inline
other_host_resolved=10.9.9.9
missing_with_default=fallback
"""


def write_tree(root: Path, template: str = _TREE_TEMPLATE) -> tuple[Path, Inventory]:
    """Write the role `widget` and its inventory under `root`.

    Returns:
        The role tree, as `render_tag`'s `roles`, and the inventory.
    """
    role = root / "roles" / "k8s" / "widget"
    (role / "templates").mkdir(parents=True)
    (role / "defaults").mkdir()
    (role / "tasks").mkdir()
    (role / "templates" / "configmap.yaml.j2").write_text(template)
    (role / "defaults" / "main.yml").write_text(_ROLE_DEFAULTS)
    (role / "tasks" / "main.yml").write_text(_ROLE_TASKS)
    host_vars = root / "inventory" / "host_vars"
    host_vars.mkdir(parents=True)
    (host_vars / "h1.yml").write_text(_H1_VARS)
    (host_vars / "h2.yml").write_text(_H2_VARS)
    group_vars = root / "inventory" / "group_vars"
    group_vars.mkdir()
    (group_vars / "all.yml").write_text(_GROUP_VARS)
    hosts_ini = root / "inventory" / "hosts.ini"
    hosts_ini.write_text(_HOSTS_INI)
    inventory = Inventory(
        all_vars=group_vars / "all.yml",
        host_vars=host_vars,
        plane_hosts={"k8s": "h1"},
        hosts_ini=hosts_ini,
    )
    return root / "roles" / "k8s", inventory
