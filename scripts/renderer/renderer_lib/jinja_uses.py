"""Where each secret key appears in a template's syntax tree, and the forms it passes through.

Rule 5 of `secret_checks.py` reads template sources through this walk. `jinja_aliases.py`
walks a template and finds every use of a key: a loaded name, a `lookup('vars', 'K')` with a
literal name, a `lookup('vars', X)` whose argument is not a literal, and a use of an alias
that carries one. From each use, `judge` walks up the enclosing nodes to the first one that
decides it, and records each form it passed on the way.

A use passes through a filter that keeps the value's text, a `~` concatenation, a list, tuple
or dict literal, the value branch of a conditional, `+` on a container literal, and a macro
argument, whose value the call's output carries. It is accepted at an output, a `{% call %}`,
a binding or an `is defined` test. A container alias also passes through a part selected by
a constant name, and a set-bound literal one is accepted at an `{% if %}`. Every other
enclosing node refuses it, so each use yields at most one refusal.

`docs/homelab-render.md` is the operator's page, and
`docs/target-architecture/render/r2-secret-placeholders.md` the plan.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

from jinja2 import nodes

from lib.repo_paths import ANSIBLE, K8S_ROLES, SHARED_TPL

TRUTHINESS = "truthiness test in an if"
CONTAINER = "container literal"
LENGTH_TEST = "length test in an if"
MACRO_ARGUMENT = "macro argument"
PART = "part"

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


class Alias(NamedTuple):
    """How a use reached its key through a name, rather than naming the key itself.

    Attributes:
        form: The first form of each use, such as `value alias x` or `output of macro m`.
        container: The name holds a list, tuple or dict that carries the key, so a part
            selected from it by a constant name is a container alias too.
        listed: `{% set %}` bound the name to a literal container, so an `{% if %}` on it
            reads only its length.
    """

    form: str
    container: bool = False
    listed: bool = False


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


def vars_lookup(node: nodes.Node) -> nodes.Expr | None:
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


def macro_names(tree: nodes.Template) -> set[str]:
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
    alias: Alias | None = None,
) -> Use:
    """Walk up from one key use through `ancestors`, innermost last, to the form that decides.

    Args:
        key: The key, or the lookup text for a non-literal `vars` lookup.
        ancestors: `(parent, field)` pairs from the template root down to the use.
        macros: The names the template binds to macros.
        policy: The pass-through filters and the multi-line keys.
        dynamic: The use is a non-literal `vars` lookup, held to the identity forms.
        alias: The name the use reached the key through, when it is not the key itself.
    """
    forms: list[str] = [alias.form] if alias else []
    for parent, fname in reversed(ancestors):
        if isinstance(parent, nodes.Keyword):
            continue
        # A part of a container alias is one, until a form turns it into text.
        whole = alias is not None and alias.container and set(forms[1:]) <= {PART}
        if whole and fname == "node" and _selects_part(parent):
            forms.append(PART)
            continue
        if alias and alias.listed and len(forms) == 1:
            if isinstance(parent, nodes.If) and fname == "test":
                return Use(key, (*forms, LENGTH_TEST), False)
        if isinstance(parent, nodes.Filter):
            if fname == "node":
                form = f"filter {parent.name}"
                if _keeps(parent.name, key, policy, dynamic):
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
        if isinstance(parent, nodes.Add) and (CONTAINER in forms or whole):
            forms.append("container join")
            continue
        # The call's output carries the argument, so the call's own context judges it.
        if _macro_call(parent, fname, macros) and not dynamic:
            forms.append(MACRO_ARGUMENT)
            continue
        accepted = _terminal(parent, fname, dynamic)
        if accepted == "output":
            return _through_blocks(key, [*forms, accepted], ancestors, policy, dynamic)
        if accepted:
            return Use(key, (*forms, accepted), False)
        return Use(key, (*forms, _refused_form(parent, fname)), True)
    return Use(key, (*forms, "template root"), True)


def _selects_part(parent: nodes.Node) -> bool:
    """Whether `parent` selects a part by a constant attribute name or string key."""
    if isinstance(parent, nodes.Getattr):
        return True
    return (
        isinstance(parent, nodes.Getitem)
        and isinstance(parent.arg, nodes.Const)
        and isinstance(parent.arg.value, str)
    )


def _macro_call(parent: nodes.Node, fname: str, macros: set[str]) -> bool:
    """Whether a use in `parent`'s field `fname` is an argument of a call to a macro."""
    return (
        isinstance(parent, nodes.Call)
        and fname in ("args", "kwargs")
        and isinstance(parent.node, nodes.Name)
        and parent.node.name in macros
    )


def _keeps(name: str, key: str, policy: Policy, dynamic: bool) -> bool:
    """Whether filter `name` leaves the value of `key` intact."""
    return name in IDENTITY_FILTERS or (
        name == "indent" and key not in policy.multi_line and not dynamic
    )


def block_filters(ancestors: list[tuple[nodes.Node, str]]) -> list[str]:
    """The filters every enclosing `{% filter %}` block or filtered `{% set %}` block applies.

    Such a block filters the text its body writes, so an output inside it passes through
    each of them.
    """
    names = []
    for parent, fname in reversed(ancestors):
        chain = None
        if fname == "body" and isinstance(
            parent, nodes.FilterBlock | nodes.AssignBlock
        ):
            chain = parent.filter
        while isinstance(chain, nodes.Filter):
            names.append(chain.name)
            chain = chain.node
    return names


def _through_blocks(
    key: str,
    forms: list[str],
    ancestors: list[tuple[nodes.Node, str]],
    policy: Policy,
    dynamic: bool,
) -> Use:
    """An output's use, held to the filters of the blocks that enclose it."""
    for name in block_filters(ancestors):
        form = f"block filter {name}"
        if not _keeps(name, key, policy, dynamic):
            return Use(key, (*forms, form), True)
        forms.append(form)
    return Use(key, tuple(forms), False)


def _terminal(parent: nodes.Node, fname: str, dynamic: bool) -> str | None:
    """The accepted form that ends a walk at `parent`, or None when `parent` refuses it."""
    if isinstance(parent, nodes.Output):
        return "output"
    # A `{% call %}` writes its macro's output where it stands.
    if isinstance(parent, nodes.CallBlock) and fname == "call":
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


class Embed(NamedTuple):
    """An `{% include %}` or `lookup('template', ...)` of another template.

    Attributes:
        kind: `include` or `lookup`.
        target: The include's name, or the lookup's path with `playbook_dir/` for that
            variable. None when the target is not a literal, so it cannot be resolved.
        use: The embed judged as a use of the keys its target names.
    """

    kind: str
    target: str | None
    use: Use


@dataclass
class TemplateScan:
    """Every use of a secret key in one template.

    Attributes:
        names_key: The tree names a secret key, as a loaded name or a literal `vars` lookup.
        uses: Every use, accepted or refused, in tree order.
        lookup_sites: The argument text of every non-literal `vars` lookup, allowlisted or not.
        keys: The keys the tree names, itself or through an alias.
        embeds: Every include and template lookup.
        bindings: `(name, is an alias)` for each name a binding in the tree sets.
    """

    names_key: bool = False
    uses: list[Use] = field(default_factory=list)
    lookup_sites: list[str] = field(default_factory=list)
    keys: set[str] = field(default_factory=set)
    embeds: list[Embed] = field(default_factory=list)
    bindings: list[tuple[str, bool]] = field(default_factory=list)


def _template_lookup(node: nodes.Node) -> tuple[str | None] | None:
    """`(path,)` when `node` is `lookup('template', path)`, with `playbook_dir/` for that name."""
    if not (
        isinstance(node, nodes.Call)
        and isinstance(node.node, nodes.Name)
        and node.node.name == "lookup"
        and len(node.args) > 1
        and isinstance(node.args[0], nodes.Const)
        and node.args[0].value == "template"
    ):
        return None
    path = node.args[1]
    if isinstance(path, nodes.Const):
        return (str(path.value),)
    if (
        isinstance(path, nodes.Add | nodes.Concat)
        and len(
            parts := (
                [path.left, path.right] if isinstance(path, nodes.Add) else path.nodes
            )
        )
        == 2
        and isinstance(parts[0], nodes.Name)
        and parts[0].name == "playbook_dir"
        and isinstance(parts[1], nodes.Const)
    ):
        return ("playbook_dir/" + str(parts[1].value).lstrip("/"),)
    return (None,)


def embed_of(
    node: nodes.Node, ancestors, macros: set[str], policy: Policy
) -> Embed | None:
    """The embed `node` is, judged with the forms its output passes through, or None."""
    if isinstance(node, nodes.Include):
        name = node.template.value if isinstance(node.template, nodes.Const) else None
        use = _through_blocks(f"include {name}", ["include"], ancestors, policy, False)
        return Embed("include", name, use)
    lookup = _template_lookup(node)
    if lookup is None:
        return None
    (target,) = lookup
    return Embed(
        "lookup", target, judge(f"template {target}", ancestors, macros, policy)
    )


def rule5_templates(roles: Path = K8S_ROLES, shared: Path = SHARED_TPL) -> list[Path]:
    """Every template rule 5 reads: each `.j2` but `*.sh.j2` under the two template roots."""
    paths = [*roles.glob("*/templates/**/*.j2"), *shared.glob("**/*.j2")]
    return sorted(p for p in paths if not p.name.endswith(".sh.j2"))


def template_tag(path: Path, roles: Path = K8S_ROLES) -> str:
    """The role a template belongs to, or `shared` for `ansible/templates/`."""
    return path.relative_to(roles).parts[0] if path.is_relative_to(roles) else "shared"


def resolve_embed(
    path: Path,
    embed: Embed,
    roles: Path = K8S_ROLES,
    shared: Path = SHARED_TPL,
    playbook_dir: Path = ANSIBLE,
) -> Path | None:
    """The template `embed` in `path` reads, as the renderer resolves it, or None.

    An include searches the role's `templates/`, then the shared directory. A template
    lookup reads `playbook_dir/<path>`, or a relative path under the role's `templates/`.
    """
    if embed.target is None:
        return None
    own = roles / template_tag(path, roles) / "templates"
    if embed.kind == "include":
        candidates = [own / embed.target, shared / embed.target]
    elif embed.target.startswith("playbook_dir/"):
        candidates = [playbook_dir / embed.target.removeprefix("playbook_dir/")]
    else:
        candidates = [own / embed.target]
    return next((c for c in candidates if c.is_file()), None)


def embed_refusals(
    scans: dict[Path, TemplateScan],
    resolve: Callable[[Path, Embed], Path | None],
    multi_line: frozenset[str],
) -> list[tuple[Path, str, str]]:
    """`(template, key, form)` for each key an embed carries through a form that changes it.

    The embedded template's uses are judged in its own file as if it stood alone, so the
    filters its embedder applies are judged here. An embed of a template that names a key
    is refused on a refused form, and on `indent` when the key is in `multi_line`. An embed
    whose target is not a literal is refused on a refused form alone.
    """
    named: dict[Path, set[str]] = {}

    def keys_of(path: Path, seen: frozenset[Path] = frozenset()) -> set[str]:
        if path not in named:
            found = set(scans[path].keys) if path in scans else set()
            for embed in scans[path].embeds if path in scans else []:
                target = resolve(path, embed)
                if target is not None and target not in seen:
                    found |= keys_of(target, seen | {path})
            named[path] = found
        return named[path]

    refusals = []
    for path, found in sorted(scans.items()):
        for embed in found.embeds:
            target = resolve(path, embed)
            keys = keys_of(target) if target is not None else {embed.use.key}
            form = embed.use.forms[-1]
            if embed.use.refused:
                refusals.extend(
                    (path, key, f"embeds through {form}") for key in sorted(keys)
                )
            elif {"filter indent", "block filter indent"} & set(embed.use.forms):
                refusals.extend(
                    (path, key, "embeds through indent")
                    for key in sorted(keys & multi_line)
                )
    return refusals
