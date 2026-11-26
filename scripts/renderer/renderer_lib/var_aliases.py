"""Rule 5's vars-file clause, and the named sites rule 5's template clause reads.

A key could reach a template through a variable whose value names it, and every template that
transformed the variable would then transform the key. `vars_file_refusals` closes that path:
no Jinja string in the k8s roles' `defaults/main.yml` or the inventory's vars files may name a
secret key, bare or through a `vars` lookup. A template reads a key by its own name, where the
template clause judges it.

`NON_SECRET_VARS_LOOKUPS` lives here too: the non-literal `vars` lookups in templates that
read no key, which rule 5 does not count as a secret reference.

`jinja_aliases.py` holds the template clause.

`docs/homelab-render.md` is the operator's page, and
`docs/archive/render/r2-secret-placeholders.md` the plan.
"""

from collections.abc import Callable, Iterable
from pathlib import Path
from typing import NamedTuple

import jinja2
from jinja2 import nodes

from lib import yaml_fast
from lib.estate import REPO_INVENTORY
from lib.repo_paths import K8S_ROLES
from renderer.renderer_lib.jinja_uses import lookup_argument, vars_lookup


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


# Each entry fails the check when its site has gone or a name its argument can take is a secret key;
# the tree holds no such lookup.
NON_SECRET_VARS_LOOKUPS: frozenset[VarsLookupSite] = frozenset()

# The `observability_prometheus_config` block holds no key, and the Deployment hashes it with
# `hash('sha1')`, which must not make it an alias.
NOT_ALIASES: tuple[tuple[str, str], ...] = (
    (
        "k8s/roles/observability/templates/prometheus.yaml.j2",
        "observability_prometheus_config",
    ),
)


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


def rule5_var_files() -> list[Path]:
    """The k8s roles' `defaults/main.yml` and every inventory vars file."""
    return sorted([*K8S_ROLES.glob("*/defaults/main.yml"), *REPO_INVENTORY.var_files()])


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


def vars_file_refusals(
    keys: frozenset[str], files: list[Path], env: jinja2.Environment
) -> list[tuple[Path, str, str]]:
    """`(file, key, "variable: form")` for each secret key a Jinja string in `files` names.

    A string names a key when it reads the key as a variable or through a literal
    `lookup('vars', ...)`. A non-literal `vars` lookup counts as naming its argument, because
    nothing shows which variable it reads.
    """
    refused = []
    for path in files:
        data = yaml_fast.safe_load(path.read_text()) or {}
        for name, value in data.items() if isinstance(data, dict) else ():
            for text in _jinja_strings(value):
                refused += [
                    (path, key, f"{name}: {form}")
                    for key, form in _named_keys(env.parse(text), keys)
                ]
    return refused


def _named_keys(
    tree: nodes.Template, keys: frozenset[str]
) -> Iterable[tuple[str, str]]:
    for node in tree.find_all((nodes.Name, nodes.Call)):
        if isinstance(node, nodes.Name) and node.ctx == "load" and node.name in keys:
            yield node.name, "named in a vars file"
            continue
        argument = vars_lookup(node)
        if isinstance(argument, nodes.Const):
            if argument.value in keys:
                yield argument.value, "vars lookup in a vars file"
        elif argument is not None:
            yield lookup_argument(argument), "non-literal vars lookup in a vars file"
