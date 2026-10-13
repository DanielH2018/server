"""Where each secret key appears in a template's syntax tree, and the forms it passes through.

Rule 5 of `secret_checks.py` reads template sources through this walk. `scan` parses a template
and finds every use of a key: a loaded name, a `lookup('vars', 'K')` with a literal name, and
a `lookup('vars', X)` whose argument is not a literal. From each use, `judge` walks up the
enclosing nodes to the first one that decides it, and records each form it passed on the way.

A use passes through a filter that keeps the value's text, a `~` concatenation, a list, tuple
or dict literal, the value branch of a conditional, and `+` on a container literal. It is
accepted at an output, a binding, an `is defined` test or a macro argument. Every other
enclosing node refuses it, so each use yields at most one refusal.

`docs/homelab-render.md` is the operator's page, and
`docs/target-architecture/render/r2-secret-placeholders.md` the plan.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from dataclasses import dataclass, field
from typing import NamedTuple

import jinja2
from jinja2 import nodes

TRUTHINESS = "truthiness test in an if"
CONTAINER = "container literal"

# How a use reached through a non-literal `vars` lookup names its key.
DYNAMIC_LOOKUP = "lookup('vars', {})"

# The filters that keep a value's text intact when the value is their subject. `indent` is
# allowed only on a key outside the multi-line set, and a non-literal lookup takes only these.
IDENTITY_FILTERS = frozenset({"default", "to_json"})
_CONTAINERS = (nodes.List, nodes.Tuple, nodes.Dict, nodes.Pair)


class Use(NamedTuple):
    """One use of a secret key in a template, and the forms it passes through.

    Attributes:
        key: The key, or `lookup('vars', X)` for a non-literal lookup.
        forms: Each enclosing form, innermost first, ending at the one that decides.
        refused: True when the last form is one rule 5 refuses.
    """

    key: str
    forms: tuple[str, ...]
    refused: bool


@dataclass(frozen=True)
class Policy:
    """What the walk allows beyond the identity forms.

    Attributes:
        passthrough: The filters whose output may carry a key passed as an argument.
        multi_line: The keys `indent` may not take, because no placeholder can carry them.
    """

    passthrough: frozenset[str]
    multi_line: frozenset[str]


def lookup_argument(node: nodes.Node) -> str:
    """The source text of a `vars` lookup's argument: a name, attribute or constant subscript."""
    if isinstance(node, nodes.Name):
        return node.name
    if isinstance(node, nodes.Getattr):
        return f"{lookup_argument(node.node)}.{node.attr}"
    if isinstance(node, nodes.Getitem) and isinstance(node.arg, nodes.Const):
        return f"{lookup_argument(node.node)}[{node.arg.value!r}]"
    if isinstance(node, nodes.Const):
        return repr(node.value)
    return f"<{type(node).__name__}>"


def _vars_lookup(node: nodes.Node) -> nodes.Expr | None:
    """The argument of `lookup('vars', X)` when `node` is that call."""
    if (
        isinstance(node, nodes.Call)
        and isinstance(node.node, nodes.Name)
        and node.node.name == "lookup"
        and len(node.args) > 1
        and isinstance(node.args[0], nodes.Const)
        and node.args[0].value == "vars"
    ):
        return node.args[1]
    return None


def _macro_names(tree: nodes.Template) -> set[str]:
    """The names a template binds to macros, its own or imported by name."""
    names = {m.name for m in tree.find_all(nodes.Macro)}
    for imp in tree.find_all(nodes.FromImport):
        names.update(n[1] if isinstance(n, tuple) else n for n in imp.names)
    return names


def judge(
    key: str,
    ancestors: list[tuple[nodes.Node, str]],
    macros: set[str],
    policy: Policy,
    dynamic: bool = False,
) -> Use:
    """Walk up from one key use through `ancestors`, innermost last, to the form that decides.

    Args:
        key: The key, or the lookup text for a non-literal `vars` lookup.
        ancestors: `(parent, field)` pairs from the template root down to the use.
        macros: The names the template binds to macros.
        policy: The pass-through filters and the multi-line keys.
        dynamic: The use is a non-literal `vars` lookup, held to the identity forms.
    """
    forms: list[str] = []
    for parent, fname in reversed(ancestors):
        if isinstance(parent, nodes.Keyword):
            continue
        if isinstance(parent, nodes.Filter):
            if fname == "node":
                form = f"filter {parent.name}"
                if parent.name in IDENTITY_FILTERS or (
                    parent.name == "indent"
                    and key not in policy.multi_line
                    and not dynamic
                ):
                    forms.append(form)
                    continue
                return Use(key, (*forms, form), True)
            form = f"argument to {parent.name}"
            if parent.name in policy.passthrough and not dynamic:
                forms.append(form)
                continue
            return Use(key, (*forms, form), True)
        if isinstance(parent, nodes.Concat) and not dynamic:
            forms.append("concatenation")
            continue
        if isinstance(parent, _CONTAINERS) and not dynamic:
            forms.append(CONTAINER)
            continue
        # A conditional's value branch passes the value on, where its test reads it.
        if isinstance(parent, nodes.CondExpr) and fname != "test" and not dynamic:
            forms.append("conditional value")
            continue
        # `+` on a container literal joins two containers and reads no character of the key.
        if isinstance(parent, nodes.Add) and CONTAINER in forms:
            forms.append("container join")
            continue
        accepted = _terminal(parent, fname, macros, dynamic)
        if accepted:
            return Use(key, (*forms, accepted), False)
        return Use(key, (*forms, _refused_form(parent, fname)), True)
    return Use(key, (*forms, "template root"), True)


def _terminal(
    parent: nodes.Node, fname: str, macros: set[str], dynamic: bool
) -> str | None:
    """The accepted form that ends a walk at `parent`, or None when `parent` refuses it."""
    if isinstance(parent, nodes.Output):
        return "output"
    if dynamic:
        return None
    if isinstance(parent, nodes.Assign) and fname == "node":
        return "set binding"
    if isinstance(parent, nodes.For) and fname == "iter":
        return "loop binding"
    if isinstance(parent, nodes.With) and fname == "values":
        return "with binding"
    defined = ("defined", "undefined")
    if isinstance(parent, nodes.Test) and fname == "node" and parent.name in defined:
        return f"test {parent.name}"
    if (
        isinstance(parent, nodes.Call)
        and fname in ("args", "kwargs")
        and isinstance(parent.node, nodes.Name)
        and parent.node.name in macros
    ):
        return "macro argument"
    return None


def _refused_form(parent: nodes.Node, fname: str) -> str:
    """How a refusal names the node `parent` that refused a use in its field `fname`."""
    if isinstance(parent, nodes.If) and fname == "test":
        return TRUTHINESS
    if isinstance(parent, nodes.CondExpr) and fname == "test":
        return "truthiness test in a conditional"
    if isinstance(parent, (nodes.Not, nodes.And, nodes.Or)):
        return "truthiness test"
    if isinstance(parent, nodes.Test):
        return f"test {parent.name}"
    if isinstance(parent, nodes.Compare | nodes.Operand):
        return "comparison"
    if isinstance(parent, nodes.Getattr):
        return "attribute"
    if isinstance(parent, nodes.Getitem):
        return "subscript"
    if isinstance(parent, nodes.Call):
        return "call argument"
    return type(parent).__name__


@dataclass
class TemplateScan:
    """Every use of a secret key in one template.

    Attributes:
        names_key: The tree names a secret key, as a loaded name or a literal `vars` lookup.
        uses: Every use, accepted or refused, in tree order.
        lookup_sites: The argument text of every non-literal `vars` lookup, allowlisted or not.
    """

    names_key: bool = False
    uses: list[Use] = field(default_factory=list)
    lookup_sites: list[str] = field(default_factory=list)


def scan(
    env: jinja2.Environment,
    source: str,
    keys: frozenset[str],
    policy: Policy,
    non_secret: frozenset[str] = frozenset(),
) -> TemplateScan:
    """Walk one template's syntax tree and judge every use of a key in `keys`.

    Args:
        env: The Jinja environment to parse with.
        source: The template's text.
        keys: The secret key names.
        policy: The pass-through filters and the multi-line keys.
        non_secret: The argument texts of the non-literal `vars` lookups in this template
            that read no secret key. They are recorded as sites and judged as nothing.
    """
    tree = env.parse(source)
    macros = _macro_names(tree)
    found = TemplateScan()

    def visit(node: nodes.Node, ancestors: list[tuple[nodes.Node, str]]) -> None:
        argument = _vars_lookup(node)
        if argument is not None:
            if isinstance(argument, nodes.Const):
                if argument.value in keys:
                    found.names_key = True
                    found.uses.append(judge(argument.value, ancestors, macros, policy))
            else:
                text = lookup_argument(argument)
                found.lookup_sites.append(text)
                if text not in non_secret:
                    name = DYNAMIC_LOOKUP.format(text)
                    found.uses.append(judge(name, ancestors, macros, policy, True))
            return
        if isinstance(node, nodes.Name) and node.ctx == "load" and node.name in keys:
            found.names_key = True
            found.uses.append(judge(node.name, ancestors, macros, policy))
            return
        for fname in node.fields:
            value = getattr(node, fname)
            for child in value if isinstance(value, list) else [value]:
                if isinstance(child, nodes.Node):
                    visit(child, [*ancestors, (node, fname)])

    visit(tree, [])
    return found
