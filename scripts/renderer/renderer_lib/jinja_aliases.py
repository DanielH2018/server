"""Rule 5's alias clauses: a name that carries a secret key is held to the key's forms.

`scan_templates` walks each template's syntax tree with the scopes Jinja gives its names. A
name bound by `{% set %}`, a set block, `{% with %}`, a `for` target or a macro parameter is
an alias when its expression carries a key that `judge` accepted at that binding. Each later
use of the alias is judged as a use of the key. A binding whose expression `judge` refused
carries nothing, so its uses add no second violation.

A *value alias* is held to the key's forms. A *container alias* is bound to a list, tuple or
dict expression or to a `PASSTHROUGH_FILTERS` member's output. It may also be iterated and
have a part selected by a constant name, and an `{% if %}` may test one that `{% set %}`
bound to a literal container. A loop over a literal list binds each target to the element,
or the tuple member, it takes.

A macro call is a use too. A call of a macro whose body outputs a key, a macro of its own
template or one imported with `{% from %}`, is a use of that key at the call. A `{% call %}`
body that outputs a key makes each `caller()` in the macro a use of it. A key passed as a
macro argument is judged where the call's output goes, and binds the parameter in the
macro's body. These facts cross templates, so `scan_templates` repeats its walk until they
stop growing.

`var_aliases.py` holds the second clause, over role defaults and inventory, and both
clauses' named members.

`docs/homelab-render.md` is the operator's page, and
`docs/target-architecture/render/r2-secret-placeholders.md` the plan.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

import jinja2
from jinja2 import nodes

from renderer.renderer_lib.jinja_uses import (
    DYNAMIC_LOOKUP,
    MACRO_ARGUMENT,
    Alias,
    Embed,
    Policy,
    TemplateScan,
    Use,
    embed_of,
    judge,
    lookup_argument,
    macro_names,
    resolve_embed,
    vars_lookup,
)

_BINDING_TERMINALS = {
    nodes.Assign: "set binding",
    nodes.For: "loop binding",
    nodes.With: "with binding",
}


class Flow(NamedTuple):
    """One key a name carries.

    Attributes:
        key: The key, or `lookup('vars', X)` for a non-literal lookup.
        dynamic: The key is a non-literal lookup, held to the identity forms.
        param: The name reached the key through a macro parameter or `caller()`, so the
            macro's output carries it only at the calls that pass it.
    """

    key: str
    dynamic: bool
    param: bool = False


class Binding(NamedTuple):
    """The keys an alias carries, and its shape as `Alias` describes it."""

    flows: frozenset[Flow]
    container: bool
    listed: bool = False

    def merge(self, other: "Binding | None") -> "Binding":
        if other is None:
            return self
        return Binding(
            self.flows | other.flows,
            self.container or other.container,
            self.listed and other.listed,
        )


MacroRef = tuple[Path, str]


@dataclass
class Facts:
    """What the walks learn about macros, keyed by the template that defines each one.

    Attributes:
        carries: The keys each macro's own body outputs.
        params: The binding each `(macro, slot)` receives, a slot being a position or a
            keyword.
        callers: The keys a `{% call %}` body hands to each macro's `caller()`.
    """

    carries: dict[MacroRef, frozenset[Flow]] = field(default_factory=dict)
    params: dict[tuple[MacroRef, int | str], Binding] = field(default_factory=dict)
    callers: dict[MacroRef, frozenset[Flow]] = field(default_factory=dict)

    def snapshot(self) -> tuple:
        return (dict(self.carries), dict(self.params), dict(self.callers))


class _Record(NamedTuple):
    """One judged use, kept so a binding can read which keys reached it intact."""

    use: Use
    flow: Flow
    node: nodes.Node
    ancestors: tuple[tuple[nodes.Node, str], ...]
    aliased: bool


def _names(target: nodes.Node) -> list[str]:
    """The names a binding target sets, in order."""
    if isinstance(target, nodes.Name):
        return [target.name]
    if isinstance(target, nodes.Tuple | nodes.List):
        return [n for item in target.items for n in _names(item)]
    return []


def _index(items: Sequence[nodes.Node], child: nodes.Node) -> int | None:
    return next((i for i, item in enumerate(items) if item is child), None)


def _under(record: _Record, depth: int, node: nodes.Node, fname: str) -> bool:
    """Whether `record`'s use sits in `node`'s field `fname`, `node` being at `depth`.

    Jinja's nodes compare by their fields, so a node is matched by identity.
    """
    if len(record.ancestors) <= depth:
        return False
    parent, field_name = record.ancestors[depth]
    return parent is node and field_name == fname


def _literal_container(expr: nodes.Node | None) -> bool:
    """Whether `expr` is a list, tuple or dict expression, joined or chosen by `+` or `if`."""
    if isinstance(expr, nodes.List | nodes.Tuple | nodes.Dict):
        return True
    if isinstance(expr, nodes.Add):
        return _literal_container(expr.left) and _literal_container(expr.right)
    if isinstance(expr, nodes.CondExpr):
        return _literal_container(expr.expr1) and (
            expr.expr2 is None or _literal_container(expr.expr2)
        )
    return False


class _Walk:
    """One pass over one template, with its scopes, judging every use and learning `Facts`."""

    def __init__(self, path, tree, keys, policy, non_secret, facts, resolve, aliases):
        self.path, self.keys, self.policy = path, keys, policy
        self.non_secret, self.facts = non_secret, facts
        self.macros = macro_names(tree)
        self.local = {m.name for m in tree.find_all(nodes.Macro)}
        self.imports: dict[str, MacroRef] = {}
        for imp in tree.find_all(nodes.FromImport):
            name = imp.template.value if isinstance(imp.template, nodes.Const) else None
            target = resolve(path, name) if name else None
            for entry in imp.names:
                macro, local = entry if isinstance(entry, tuple) else (entry, entry)
                if target is not None:
                    self.imports[local] = (target, macro)
        self.scopes: list[dict[str, Binding | None]] = [dict(aliases), {}]
        self.contexts: list[tuple[str, MacroRef]] = []
        self.records: list[_Record] = []
        self.found = TemplateScan()
        self.visit(tree, [])

    # -- names and uses ------------------------------------------------------------------

    def lookup(self, name: str, scopes=None) -> Binding | None:
        for scope in reversed(self.scopes if scopes is None else scopes):
            if name in scope:
                return scope[name]
        return None

    def macro_ref(self, node: nodes.Node) -> MacroRef | None:
        if not isinstance(node, nodes.Name):
            return None
        if node.name in self.imports:
            return self.imports[node.name]
        return (self.path, node.name) if node.name in self.local else None

    def use(self, flow: Flow, node, ancestors, alias: Alias | None = None) -> None:
        used = judge(flow.key, ancestors, self.macros, self.policy, flow.dynamic, alias)
        self.found.uses.append(used)
        if not flow.dynamic:
            self.found.keys.add(flow.key)
        self.records.append(
            _Record(used, flow, node, tuple(ancestors), alias is not None)
        )
        if used.refused or "output" not in used.forms or flow.param:
            return
        for kind, ref in self.contexts:
            table = self.facts.carries if kind == "macro" else self.facts.callers
            table[ref] = table.get(ref, frozenset()) | {flow}

    def use_alias(self, binding: Binding, node, ancestors, form: str) -> None:
        alias = Alias(form, binding.container, binding.listed)
        for flow in sorted(binding.flows):
            self.use(flow, node, ancestors, alias)

    def name_use(self, name: str, node, ancestors) -> None:
        if name in self.keys:
            self.found.names_key = True
            self.use(Flow(name, False), node, ancestors)
            return
        binding = self.lookup(name)
        if binding is not None:
            shape = "container" if binding.container else "value"
            self.use_alias(binding, node, ancestors, f"{shape} alias {name}")

    # -- the walk --------------------------------------------------------------------------

    def visit(self, node: nodes.Node, ancestors: list) -> None:
        embed = embed_of(node, ancestors, self.macros, self.policy)
        if embed is not None:
            self.found.embeds.append(embed)
        if isinstance(node, nodes.Name):
            if node.ctx == "load":
                self.name_use(node.name, node, ancestors)
            return
        argument = vars_lookup(node)
        if argument is not None:
            self.lookup_use(node, argument, ancestors)
            return
        handlers: dict[type, Callable[[Any, list], None]] = {
            nodes.For: self.loop,
            nodes.If: self.branch,
            nodes.Assign: self.assign,
            nodes.AssignBlock: self.assign_block,
            nodes.With: self.with_block,
            nodes.Macro: self.macro,
            nodes.CallBlock: self.call_block,
            nodes.Call: self.call,
        }
        handlers.get(type(node), self.children)(node, ancestors)

    def children(self, node: nodes.Node, ancestors: list, fields=None) -> None:
        for fname in node.fields if fields is None else fields:
            value = getattr(node, fname)
            for child in value if isinstance(value, list) else [value]:
                if isinstance(child, nodes.Node):
                    self.visit(child, [*ancestors, (node, fname)])

    def lookup_use(self, node, argument, ancestors) -> None:
        if isinstance(argument, nodes.Const):
            self.name_use(argument.value, node, ancestors)
            return
        text = lookup_argument(argument)
        self.found.lookup_sites.append(text)
        if text not in self.non_secret:
            self.use(Flow(DYNAMIC_LOOKUP.format(text), True), node, ancestors)

    def call(self, node: nodes.Call, ancestors: list) -> None:
        ref = self.macro_ref(node.node)
        if ref is not None:
            flows = self.facts.carries.get(ref, frozenset())
            name = f"output of macro {ref[1]}"
            self.use_alias(Binding(flows, False), node, ancestors, name)
        elif isinstance(node.node, nodes.Name) and node.node.name == "caller":
            macro = next((r for k, r in reversed(self.contexts) if k == "macro"), None)
            flows = self.facts.callers.get(macro, frozenset()) if macro else ()
            held = Binding(frozenset(f._replace(param=True) for f in flows), False)
            self.use_alias(held, node, ancestors, "output of caller")
        start = len(self.records)
        self.children(node, ancestors)
        if ref is not None:
            self.learn_params(node, ancestors, ref, self.records[start:])

    def learn_params(self, call, ancestors, ref: MacroRef, records) -> None:
        """Record the binding each argument slot of `call` gives the macro's parameter."""
        depth = len(ancestors)
        for r in records:
            if len(r.ancestors) <= depth or r.ancestors[depth][0] is not call:
                continue
            below = r.ancestors[depth + 1 :]
            # Each enclosing node but a keyword adds one form, so this is the call's form.
            at = int(r.aliased) + sum(
                not isinstance(n, nodes.Keyword) for n, _ in below
            )
            if r.use.forms[at : at + 1] != (MACRO_ARGUMENT,):
                continue
            arg = below[0][0] if below else r.node
            if isinstance(arg, nodes.Keyword):
                slot, expr = arg.key, arg.value
            else:
                slot, expr = _index(call.args, arg), arg
            if slot is None:
                continue
            held = Binding(
                frozenset({r.flow._replace(param=True)}), self.container(expr)
            )
            self.facts.params[(ref, slot)] = held.merge(
                self.facts.params.get((ref, slot))
            )

    def container(self, expr: nodes.Node | None) -> bool:
        """Whether `expr` is a container, so a name bound to it is a container alias."""
        if _literal_container(expr):
            return True
        if isinstance(expr, nodes.Add):
            return self.container(expr.left) or self.container(expr.right)
        if isinstance(expr, nodes.CondExpr):
            return self.container(expr.expr1) or (
                expr.expr2 is not None and self.container(expr.expr2)
            )
        if isinstance(expr, nodes.Filter):
            if expr.name in self.policy.passthrough:
                return True
            return expr.name == "default" and self.container(expr.node)
        if isinstance(expr, nodes.Getattr | nodes.Getitem):
            return self.container(expr.node)
        if isinstance(expr, nodes.Name):
            binding = self.lookup(expr.name)
            return binding is not None and binding.container
        return False

    def bound(self, records, node, ancestors, fname, split) -> dict[str, Binding]:
        """The binding each name gets from the records that reached `node`'s `fname`.

        `split(chain)` maps one record's chain of nodes, from the field's expression down to
        the use, onto `(name, container)` pairs.
        """
        depth, terminal = len(ancestors), _BINDING_TERMINALS[type(node)]
        out: dict[str, Binding] = {}
        for r in records:
            if r.use.refused or r.use.forms[-1] != terminal:
                continue
            if not _under(r, depth, node, fname):
                continue
            chain = [n for n, _ in r.ancestors[depth + 1 :]] + [r.node]
            for name, container in split(chain):
                out[name] = Binding(frozenset({r.flow}), container).merge(out.get(name))
        return out

    def bind(self, names: list[str], bound: dict[str, Binding], scope=None) -> None:
        scope = self.scopes[-1] if scope is None else scope
        for name in names:
            scope[name] = bound.get(name)
            self.found.bindings.append((name, name in bound))

    def unpack(self, target, expr, chain_rest) -> list[tuple[str, bool]]:
        """`(name, container)` for each name `target` takes from `expr`."""
        names = _names(target)
        if isinstance(target, nodes.Name):
            return [(target.name, self.container(expr))]
        if isinstance(expr, nodes.Tuple | nodes.List) and len(expr.items) == len(names):
            i = _index(expr.items, chain_rest[0]) if chain_rest else None
            if i is not None:
                return [(names[i], self.container(expr.items[i]))]
        return [(name, True) for name in names]

    def assign(self, node: nodes.Assign, ancestors: list) -> None:
        start = len(self.records)
        self.visit(node.node, [*ancestors, (node, "node")])
        bound = self.bound(
            self.records[start:],
            node,
            ancestors,
            "node",
            lambda chain: self.unpack(node.target, chain[0], chain[1:]),
        )
        if isinstance(node.target, nodes.Name) and node.target.name in bound:
            listed = _literal_container(node.node)
            bound[node.target.name] = bound[node.target.name]._replace(listed=listed)
        self.bind(_names(node.target), bound)

    def assign_block(self, node: nodes.AssignBlock, ancestors: list) -> None:
        self.children(node, ancestors, ("filter",))
        start, depth = len(self.records), len(ancestors)
        self.children(node, ancestors, ("body",))
        flows = frozenset(
            r.flow
            for r in self.records[start:]
            if not r.use.refused
            and "output" in r.use.forms
            and _under(r, depth, node, "body")
        )
        bound = {n: Binding(flows, False) for n in _names(node.target) if flows}
        self.bind(_names(node.target), bound)

    def loop(self, node: nodes.For, ancestors: list) -> None:
        start = len(self.records)
        self.visit(node.iter, [*ancestors, (node, "iter")])

        def split(chain):
            # Over a literal list, a target takes the shape of the element it takes.
            i = 0
            while isinstance(chain[i], nodes.Add | nodes.CondExpr) and i + 1 < len(
                chain
            ):
                i += 1
            if isinstance(chain[i], nodes.List) and i + 1 < len(chain):
                return self.unpack(node.target, chain[i + 1], chain[i + 2 :])
            whole = self.container(node.iter)
            return [(name, whole) for name in _names(node.target)]

        bound = self.bound(self.records[start:], node, ancestors, "iter", split)
        self.scopes.append({})
        self.bind(_names(node.target), bound)
        self.children(node, ancestors, ("test", "body"))
        self.scopes.pop()
        self.children(node, ancestors, ("else_",))

    def with_block(self, node: nodes.With, ancestors: list) -> None:
        start = len(self.records)
        self.children(node, ancestors, ("values",))

        def split(chain):
            i = _index(node.values, chain[0])
            if i is None:
                return []
            return self.unpack(node.targets[i], chain[0], chain[1:])

        bound = self.bound(self.records[start:], node, ancestors, "values", split)
        self.scopes.append({})
        self.bind([n for t in node.targets for n in _names(t)], bound)
        self.children(node, ancestors, ("body",))
        self.scopes.pop()

    def branch(self, node: nodes.If, ancestors: list) -> None:
        """Walk each branch from the same scope, then keep every alias any branch bound."""
        before, results = self.scopes[-1], []
        arms = [(node, ancestors)]
        arms += [(e, [*ancestors, (node, "elif_")]) for e in node.elif_]
        for arm, path in arms:
            self.scopes[-1] = dict(before)
            self.children(arm, path, ("test", "body"))
            results.append(self.scopes[-1])
        self.scopes[-1] = dict(before)
        self.children(node, ancestors, ("else_",))
        results.append(self.scopes[-1])
        merged: dict[str, Binding | None] = {}
        for name in set().union(*results):
            found = [
                r[name] if name in r else self.lookup(name, self.scopes[:-1])
                for r in results
            ]
            held = [b for b in found if b is not None]
            for other in held[1:]:
                held[0] = held[0].merge(other)
            merged[name] = held[0] if held else None
        self.scopes[-1] = merged

    def macro(self, node: nodes.Macro, ancestors: list) -> None:
        ref = (self.path, node.name)
        params = {}
        for i, arg in enumerate(node.args):
            by_slot = self.facts.params.get((ref, i))
            by_name = self.facts.params.get((ref, arg.name))
            params[arg.name] = by_slot.merge(by_name) if by_slot else by_name
        self.children(node, ancestors, ("defaults",))
        self.scopes.append(params)
        self.contexts.append(("macro", ref))
        self.children(node, ancestors, ("body",))
        self.contexts.pop()
        self.scopes.pop()

    def call_block(self, node: nodes.CallBlock, ancestors: list) -> None:
        self.children(node, ancestors, ("call", "defaults"))
        ref = self.macro_ref(node.call.node)
        self.scopes.append({arg.name: None for arg in node.args})
        self.contexts += [("caller", ref)] if ref else []
        self.children(node, ancestors, ("body",))
        self.contexts = self.contexts[:-1] if ref else self.contexts
        self.scopes.pop()


def resolve_import(path: Path, name: str) -> Path | None:
    """The template a `{% from name import ... %}` in `path` reads, as an include resolves."""
    return resolve_embed(path, Embed("include", name, Use(name, (), False)))


def scan_templates(
    sources: dict[Path, str],
    keys: frozenset[str],
    policy: Policy,
    env: jinja2.Environment,
    non_secret: dict[Path, frozenset[str]] | None = None,
    resolve: Callable[[Path, str], Path | None] = resolve_import,
    aliases: dict[str, Binding] | None = None,
) -> dict[Path, TemplateScan]:
    """Judge every use of a key in `sources`, their aliases and macros included.

    Args:
        sources: Each template's text, by path.
        keys: The secret key names.
        policy: The pass-through filters and the multi-line keys.
        env: The Jinja environment to parse with.
        non_secret: Per template, the argument texts of its non-literal `vars` lookups that
            read no secret key. They are recorded as sites and judged as nothing.
        resolve: The template a `{% from %}` names, from the template that imports it.
        aliases: The names every template sees as aliases, from `var_aliases`.
    """
    trees = {path: env.parse(text) for path, text in sources.items()}
    facts, non_secret = Facts(), non_secret or {}
    # Facts only grow, so the walk settles. The last pass ran with every fact it learned.
    for _ in range(20):
        before = facts.snapshot()
        scans = {
            path: _Walk(
                path,
                tree,
                keys,
                policy,
                non_secret.get(path, frozenset()),
                facts,
                resolve,
                aliases or {},
            ).found
            for path, tree in trees.items()
        }
        if facts.snapshot() == before:
            return scans
    raise RuntimeError("rule 5's macro facts did not settle in 20 passes")


def judged_flows(
    path: Path,
    tree: nodes.Template,
    keys: frozenset[str],
    policy: Policy,
    aliases: dict[str, Binding],
) -> list[tuple[Flow, Use]]:
    """`(flow, use)` for each use in one template walked alone, with no macro facts."""
    walk = _Walk(path, tree, keys, policy, (), Facts(), resolve_import, aliases)
    return [(r.flow, r.use) for r in walk.records]
