"""The Jinja constructs the renderer accepts, and the walk that names a source's constructs.

`docs/adr/0028-the-renderer-supports-a-closed-jinja-subset.md` is the specification.
`environment.renderer_env` refuses any filter, test or lookup these literals do not name, and
`scripts/renderer/tests/test_renderer_dialect.py` holds every template and variable value of
the k8s roles, and the ADR's tables, to them.
"""

from typing import NamedTuple

import jinja2
from jinja2 import nodes

# DECIDED: a closed list, read by the refusal and the census, because nothing compares the
# render with Ansible's once R4.7 deletes the play. (ADR-0028)
FILTERS = frozenset(
    {
        "authelia_service_rules",
        "basename",
        "bool",
        "capitalize",
        "combine",
        "default",
        "dictsort",
        "entry_names",
        "entry_port",
        "first",
        "flux_components",
        "format",
        "from_yaml",
        "grafana_deploy_annotated",
        "hash",
        "homelab_role_label",
        "homepage_href",
        "homepage_widget_url",
        "in_service_tier",
        "indent",
        "ingress_probe_targets",
        "int",
        "join",
        "lan_hostnames",
        "length",
        "list",
        "longhorn_backup_name",
        "longhorn_nobackup_claims",
        "longhorn_weekly_claims",
        "lower",
        "map",
        "max",
        "metrics_port",
        "min",
        "netpol_caller_fences",
        "netpol_callers",
        "published_ports",
        "reject",
        "replace",
        "reverse",
        "scrape_jobs",
        "selectattr",
        "sort",
        "string",
        "tier_backup_claims",
        "tier_priority_class",
        "to_json",
        "tojson",
        "trim",
        "unique",
        "updater_lag_grace_s",
        "upper",
        "urlencode",
        "weekly_backup_group",
    }
)
TESTS = frozenset({"defined", "equalto", "in", "none", "search"})
LOOKUPS = frozenset({"file", "pipe", "template", "vars"})

# Each filter that takes a test's name, and the position of that name among its arguments.
_TEST_ARGUMENT = {"select": 0, "reject": 0, "selectattr": 1, "rejectattr": 1}
_ANSIBLE_PREFIX = "ansible.builtin."


class Construct(NamedTuple):
    """One use of a filter, a test or a lookup.

    Attributes:
        kind: `filter`, `test` or `lookup`.
        name: The construct's name. A lookup's kind that is no literal reads `<dynamic>`.
    """

    kind: str
    name: str


def _lookup_kind(call: nodes.Call) -> str | None:
    if not (isinstance(call.node, nodes.Name) and call.node.name == "lookup"):
        return None
    kind = call.args[0] if call.args else None
    if isinstance(kind, nodes.Const):
        return str(kind.value).removeprefix(_ANSIBLE_PREFIX)
    return "<dynamic>"


def constructs(source: str) -> set[Construct]:
    """Every filter, test and lookup `source` names, a test passed by name to `reject` included.

    Raises:
        jinja2.TemplateSyntaxError: `source` does not parse.
    """
    found = set()
    for node in (
        jinja2.Environment()
        .parse(source)
        .find_all((nodes.Filter, nodes.Test, nodes.Call))
    ):
        if isinstance(node, nodes.Test):
            found.add(Construct("test", node.name))
        elif isinstance(node, nodes.Call):
            kind = _lookup_kind(node)
            if kind is not None:
                found.add(Construct("lookup", kind))
        else:
            found.add(Construct("filter", node.name))
            position = _TEST_ARGUMENT.get(node.name)
            if position is not None and len(node.args) > position:
                named = node.args[position]
                if isinstance(named, nodes.Const):
                    found.add(Construct("test", str(named.value)))
    return found


def off_the_list(source: str) -> set[Construct]:
    """The constructs `source` names that the literals do not."""
    allowed = {"filter": FILTERS, "test": TESTS, "lookup": LOOKUPS}
    return {c for c in constructs(source) if c.name not in allowed[c.kind]}
