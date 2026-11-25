"""The dialect's spec fixture for variable semantics: a role tree and its inventory.

`write_tree` writes them, and `TREE_LITERAL` is what the template renders to. A k8s
render merges no host's layer, so h1's values reach no line; a template names a host through
`hosts`. A render key ranks as the role default it is (ADR-0028's amendment).
"""

from pathlib import Path

from lib.estate import Inventory

_TREE_TEMPLATE = """\
default={{ only_default }}
group={{ group_over_default }}
host_layer_ignored={{ host_over_group }}
render_key={{ manifests_deferred_dir_name }}
derived={{ derived }}
other_host_literal={{ hosts['h2'].server_ip }}
missing_with_default={{ absent | default('fallback') }}
"""
_ROLE_DEFAULTS = """\
only_default: from-defaults
group_over_default: from-defaults
host_over_group: from-defaults
derived: "derived-{{ host_over_group }}"
manifests_deferred_dir_name: from-defaults
manifests_prune: false
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
"""
_H2_VARS = """\
server_ip: 10.9.9.9
"""
_SERVICES = "containers_list:\n  - name: widget\n"
# What `_TREE_TEMPLATE` renders to, with no extra vars. No host's layer is merged in.
TREE_LITERAL = """\
default=from-defaults
group=from-group
host_layer_ignored=from-group
render_key=from-group
derived=derived-from-group
other_host_literal=10.9.9.9
missing_with_default=fallback
"""


def write_tree(root: Path, template: str = _TREE_TEMPLATE) -> tuple[Path, Inventory]:
    """Write the role `widget` and its inventory under `root`.

    Returns:
        The role tree, as `render_tag`'s `roles`, and the inventory.
    """
    role = root / "k8s" / "roles" / "widget"
    (role / "templates").mkdir(parents=True)
    (role / "defaults").mkdir()
    (role / "templates" / "configmap.yaml.j2").write_text(template)
    (role / "defaults" / "main.yml").write_text(_ROLE_DEFAULTS)
    host_vars = root / "inventory" / "host_vars"
    host_vars.mkdir(parents=True)
    (host_vars / "h1.yml").write_text(_H1_VARS)
    (host_vars / "h2.yml").write_text(_H2_VARS)
    group_vars = root / "inventory" / "group_vars"
    group_vars.mkdir()
    (group_vars / "all.yml").write_text(_GROUP_VARS)
    services = root / "services.yml"
    services.write_text(_SERVICES)
    inventory = Inventory(
        all_vars=group_vars / "all.yml",
        host_vars=host_vars,
        services=services,
    )
    return root / "k8s" / "roles", inventory
