"""Rule 5's second alias clause, over role defaults and inventory, and both clauses' members.

A key can be reached through a variable whose value names it. `var_aliases` parses every Jinja
string in the k8s roles' `defaults/main.yml` and the inventory's vars files. A variable whose
value outputs a key, or an alias of one, is an alias of that key in every template, so a
template that transforms it fails as if it transformed the key. A transform inside the value
is a violation of its own. Plain text around an interpolation is accepted, as a `~`
concatenation is.

`NON_SECRET_VARS_LOOKUPS` lives here too: the non-literal `vars` lookups that read no key,
which neither clause counts as a secret reference.

`jinja_aliases.py` holds the template-local clause and the walk both clauses share.

`docs/homelab-render.md` is the operator's page, and
`docs/target-architecture/render/r2-secret-placeholders.md` the plan.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from collections.abc import Callable, Iterable
from pathlib import Path
from typing import NamedTuple

import jinja2

from lib import yaml_fast
from lib.render_guard import containers_entries
from lib.repo_paths import HOST_VARS, INVENTORY, K8S_ROLES
from renderer.renderer_lib.jinja_aliases import Binding, judged_flows
from renderer.renderer_lib.jinja_uses import Policy


def _replicas_vars(host_vars: Path = HOST_VARS) -> set[str]:
    """Every `replicas_var` of a `metrics:` item in any host's `containers_list`."""
    return {
        str(item["replicas_var"])
        for path in sorted(host_vars.glob("*.yml"))
        for entry in containers_entries(path)
        for item in entry.get("metrics") or []
        if isinstance(item, dict) and "replicas_var" in item
    }


class VarsLookupSite(NamedTuple):
    """A non-literal `lookup('vars', X)` that reads no secret key.

    Attributes:
        template: The template's path from the repository root.
        argument: `X` as `lookup_argument` writes it.
        names: Returns every variable name `X` can take.
    """

    template: str
    argument: str
    names: Callable[[], set[str]]


# The non-literal `vars` lookups that rule 5 does not count as a secret reference, for any of
# its clauses. Each entry fails the check when its site has gone, or when a name its argument
# can take is a secret key.
NON_SECRET_VARS_LOOKUPS = frozenset(
    {
        # `lookup('vars', job.replicas_var) | int > 0` drops a scrape job whose workload is
        # scaled to zero. `replicas_var` is a field of a `containers_list` entry's `metrics:`
        # list (ansible/filter_plugins/scrape_jobs.py), and names a replica count.
        VarsLookupSite(
            "ansible/roles/k8s/observability/templates/prometheus.yaml.j2",
            "job.replicas_var",
            _replicas_vars,
        ),
    }
)

# The uses the alias clauses must find and accept on the real tree, as
# `(template, key, form, count)`: the headlamp default, the uptime-kuma bridge row's `tags`,
# whose `{% if tags %}` is the length test, and the IngressRoute macro's loop target `host`.
ALIAS_MEMBERS = (
    (
        "ansible/roles/k8s/headlamp/templates/deployment.yaml.j2",
        "domain",
        "value alias headlamp_k8s_oidc_issuer_url",
        1,
    ),
    (
        "ansible/roles/k8s/uptime-kuma/templates/static-monitors.yaml.j2",
        "domain",
        "container alias tags",
        2,
    ),
    (
        "ansible/roles/k8s/uptime-kuma/templates/static-monitors.yaml.j2",
        "domain",
        "length test in an if",
        1,
    ),
    ("ansible/templates/ingressroute.yml.j2", "domain", "value alias host", 1),
)

# The bindings rule 5 must see and must not read as an alias, as `(template, name)`. It is
# empty because the `observability_prometheus_config` set block in `prometheus.yaml.j2`
# outputs `{{ domain }}` and so is an alias, which its `KNOWN_VIOLATIONS` entry records
# (#4952). R2's PR 8 moves the key out of that block and restores the member here.
NOT_ALIASES: tuple[tuple[str, str], ...] = ()


def not_alias_problems(
    bindings: set[tuple[str, str, bool]],
    members: tuple[tuple[str, str], ...] = NOT_ALIASES,
) -> list[str]:
    """Each `NOT_ALIASES` member that went unbound or became an alias, one line apiece.

    Args:
        bindings: `(template, name, is an alias)` for each binding rule 5 read.
        members: The `(template, name)` pairs to hold.
    """
    problems = []
    for template, name in members:
        if (template, name, True) in bindings:
            problems.append(
                f"no-secret-expression: {name} in {template} carries a secret key"
            )
        elif (template, name, False) not in bindings:
            problems.append(
                f"no-secret-expression: named member gone: no binding of {name} in "
                f"{template}"
            )
    return problems


class VarAliases(NamedTuple):
    """The second alias clause's verdict on role defaults and inventory.

    Attributes:
        bindings: Each variable whose value names a key, as the alias every template sees.
        refused: `(file, key, "variable: form")` for each transform of a key in a value.
    """

    bindings: dict[str, Binding]
    refused: list[tuple[Path, str, str]]


def rule5_var_files(roles: Path = K8S_ROLES, inventory: Path = INVENTORY) -> list[Path]:
    """The k8s roles' `defaults/main.yml` and every inventory vars file."""
    return sorted(
        [
            *roles.glob("*/defaults/main.yml"),
            *inventory.glob("group_vars/**/*.yml"),
            *inventory.glob("host_vars/**/*.yml"),
        ]
    )


def var_tag(path: Path) -> str:
    """The role a defaults file belongs to, or `inventory`."""
    return path.parent.parent.name if path.parent.name == "defaults" else "inventory"


def _jinja_strings(value) -> Iterable[str]:
    if isinstance(value, str):
        if "{{" in value or "{%" in value:
            yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _jinja_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _jinja_strings(item)


def var_aliases(
    keys: frozenset[str], policy: Policy, files: list[Path], env: jinja2.Environment
) -> VarAliases:
    """Read every Jinja string in `files`, and return the aliases and transforms it holds.

    A variable is an alias when its string, or any string nested in its list or dict,
    outputs a key or an alias. A list or dict value makes a container alias. The pass
    repeats until it adds no alias, so an alias of an alias counts.
    """
    strings = []
    for path in files:
        data = yaml_fast.safe_load(path.read_text()) or {}
        for name, value in data.items() if isinstance(data, dict) else ():
            container = not isinstance(value, str)
            for text in _jinja_strings(value):
                strings.append((path, str(name), env.parse(text), container))
    bindings: dict[str, Binding] = {}
    while True:
        found: dict[str, Binding] = {}
        refused = []
        for path, name, tree, container in strings:
            judged = judged_flows(path, tree, keys, policy, bindings)
            refused += [
                (path, use.key, f"{name}: {use.forms[-1]}")
                for _, use in judged
                if use.refused
            ]
            flows = frozenset(
                f for f, u in judged if not u.refused and "output" in u.forms
            )
            if flows:
                found[name] = Binding(flows, container).merge(found.get(name))
        if found == bindings:
            return VarAliases(bindings, refused)
        bindings = found
