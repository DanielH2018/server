"""Whether a filter plugin's change only reorders the play, read from its source (#4781).

`digest_covers.py` asks this of a changed filter plugin: a change confined to a filter whose
output only orders the k8s play moves the order roles deploy in, never what a role does, so
the render proof may discharge a line it wrote. The rule reads the plugin's syntax tree at
both ends of the range and never imports it, for the reason `filters.py` gives.

A module of its own because it reads Python where the rest of `digest_covers.py` reads YAML,
and it imports nothing from that file.
"""

import ast

# DECIDED: `toposort.py`'s `build_k8s_dep_map` reaches no role's own tasks (#4781).
# `deploy.yml` reads it only into `_k8s_container_deps_map`, which it reads only as
# `toposort_containers`' argument, and that filter reads the map only to order the entries and
# to refuse a cycle (`tests/test_order_only.py::
# test_the_play_reads_the_ordering_filters_only_to_order_itself` pins the callers). So a change
# confined to it and its own helpers moves the order roles deploy in, never what a role does,
# PROVIDED the changed code writes nothing through its arguments or a module global: an entry
# mutated in place reaches every role through `item`. `_writes_outside_itself` refuses any
# such write. `toposort_containers` is NOT order-only: its RESULT becomes `containers_list`,
# so a new body could return altered or fewer entries, and no static rule here can tell a
# reordering from that. The gap taken is a role whose bytes depend on list order; the render
# proof the caller still requires catches that one. `filter_by_platform` decides WHICH
# entries deploy, so a change reaching it keeps the line.
ORDER_ONLY_FILTERS = {
    "ansible/filter_plugins/toposort.py": frozenset({"build_k8s_dep_map"}),
}

# Methods that change their receiver in place. A changed order-only function may call them
# only on a container it built itself.
_MUTATORS = frozenset(
    {
        "append", "extend", "insert", "remove", "pop", "popitem", "clear", "update",
        "setdefault", "add", "discard", "sort", "reverse", "difference_update",
        "intersection_update", "symmetric_difference_update",
    }
)  # fmt: skip
# Calls whose result shares no mutable object with their argument: a set holds no dict, and
# the rest return scalars. `list()`, `dict()` and `sorted()` copy shallowly, so they do not
# clean the taint.
_FRESH_CALLS = frozenset({"set", "frozenset", "len", "str", "int", "bool", "repr"})


def _without_docstring(node):
    body = getattr(node, "body", None)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        node.body = body[1:]


def _top_level(text: str) -> tuple[dict[str, tuple[str, set[str]]], list[str]]:
    """A module's top-level statements, with docstrings and comments left out.

    Returns:
        `name -> (dump, names it uses)` for each statement that binds a name, and the dumps
        of the statements that bind none.
    """
    tree = ast.parse(text)
    _without_docstring(tree)
    named: dict[str, tuple[str, set[str]]] = {}
    unnamed: list[str] = []
    for stmt in tree.body:
        for node in ast.walk(stmt):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                _without_docstring(node)
        uses = {n.id for n in ast.walk(stmt) if isinstance(n, ast.Name)}
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names = [stmt.name]
        elif isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            if len(names) != len(targets):
                names = []
        elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
            names = [(a.asname or a.name).split(".")[0] for a in stmt.names]
        else:
            names = []
        if not names:
            unnamed.append(ast.dump(stmt))
        for name in names:
            named[name] = (ast.dump(stmt), uses)
    return named, unnamed


def _registered(text: str) -> dict[str, str]:
    """`FilterModule.filters()`'s literal dict as filter name -> function name."""
    for cls in ast.parse(text).body:
        if isinstance(cls, ast.ClassDef) and cls.name == "FilterModule":
            for ret in (n for n in ast.walk(cls) if isinstance(n, ast.Return)):
                if isinstance(ret.value, ast.Dict):
                    return {
                        k.value: v.id
                        for k, v in zip(ret.value.keys, ret.value.values, strict=True)
                        if isinstance(k, ast.Constant)
                        and isinstance(k.value, str)
                        and isinstance(v, ast.Name)
                    }
    return {}


def _closure(start: set[str], named: dict[str, tuple[str, set[str]]]) -> set[str]:
    seen, todo = set(), list(start)
    while todo:
        name = todo.pop()
        if name in seen or name not in named:
            continue
        seen.add(name)
        todo.extend(named[name][1])
    return seen


def _root(node) -> str | None:
    """The name an attribute, subscript or method-call chain starts from."""
    while isinstance(node, (ast.Attribute, ast.Subscript, ast.Call)):
        node = node.func if isinstance(node, ast.Call) else node.value
    return node.id if isinstance(node, ast.Name) else None


def _writes_outside_itself(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether `fn` may write to an object it did not create: an argument or a global.

    Taint starts at the parameters and flows through every assignment, loop and
    comprehension whose source names a tainted value, unless a `_FRESH_CALLS` call wraps it.
    A write is an item or attribute assignment, a `del`, an augmented assignment to a
    tainted name or a `_MUTATORS` call, each allowed only on a clean name `fn` binds
    itself. `global`, `nonlocal`, `setattr` and `delattr` count as writes outright.
    """
    args = fn.args
    tainted = {
        a.arg
        for a in [
            *args.posonlyargs,
            *args.args,
            *args.kwonlyargs,
            args.vararg,
            args.kwarg,
        ]
        if a is not None
    }
    bound: set[str] = set()

    def names(target) -> set[str]:
        return {n.id for n in ast.walk(target) if isinstance(n, ast.Name)}

    def taints(value) -> bool:
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id in _FRESH_CALLS
        ):
            return False
        return bool(names(value) & tainted)

    flows = []
    for node in ast.walk(fn):
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            return True
        if isinstance(node, ast.Assign):
            flows += [(t, node.value) for t in node.targets]
        elif (
            isinstance(node, (ast.AnnAssign, ast.AugAssign)) and node.value is not None
        ):
            flows.append((node.target, node.value))
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            flows.append((node.target, node.iter))
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            flows.append((node.optional_vars, node.context_expr))
        elif isinstance(node, ast.NamedExpr):
            flows.append((node.target, node.value))
    for target, _ in flows:
        if isinstance(target, (ast.Name, ast.Tuple, ast.List)):
            bound |= names(target)
    while True:
        grown = {
            n
            for target, value in flows
            if isinstance(target, (ast.Name, ast.Tuple, ast.List)) and taints(value)
            for n in names(target)
        } - tainted
        if not grown:
            break
        tainted |= grown

    def clean_local(node) -> bool:
        root = _root(node)
        return root is not None and root in bound and root not in tainted

    for node in ast.walk(fn):
        targets = []
        if isinstance(node, (ast.Assign, ast.Delete)):
            targets = node.targets
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        for target in targets:
            if isinstance(target, (ast.Subscript, ast.Attribute)) and not clean_local(
                target
            ):
                return True
            if isinstance(node, ast.AugAssign) and not clean_local(target):
                return True
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in {"setattr", "delattr"}:
                return True
            if (
                isinstance(func, ast.Attribute)
                and func.attr in _MUTATORS
                and not clean_local(func.value)
            ):
                return True
    return False


def _functions(text: str) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    return {
        n.name: n
        for n in ast.parse(text).body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def order_only(path: str, before: str, after: str) -> bool:
    """Whether a filter plugin's change is confined to its `ORDER_ONLY_FILTERS` (#4781).

    True when every top-level name whose definition changed is reached by none of the
    plugin's other filters, at either end, `FilterModule` itself is unchanged, and no changed
    function writes to an object it did not create (`_writes_outside_itself`). False on any
    doubt: a plugin not in the table, a parse error, a changed statement that binds no name,
    or a registration this cannot read.
    """
    order = ORDER_ONLY_FILTERS.get(path)
    if not order:
        return False
    try:
        old, old_unnamed = _top_level(before)
        new, new_unnamed = _top_level(after)
        registered = _registered(before) | _registered(after)
    except SyntaxError:
        return False
    if old_unnamed != new_unnamed or not registered:
        return False
    changed = {
        n
        for n in old.keys() | new.keys()
        if old.get(n, ("",))[0] != new.get(n, ("",))[0]
    }
    others = {fn for name, fn in registered.items() if name not in order}
    reached = _closure(others, old) | _closure(others, new) | {"FilterModule"}
    if changed & reached:
        return False
    functions = _functions(after)
    return not any(
        _writes_outside_itself(functions[n]) for n in changed if n in functions
    )
