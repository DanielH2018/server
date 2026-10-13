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
# such write, and any call that hands such an object to a callee it cannot read (#4842).
# `toposort_containers` is NOT order-only: its RESULT becomes `containers_list`,
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
# Builtins that read their arguments and call nothing they are handed, so a tainted argument
# reaches no write through them. `sorted` joins them only without a `key`, which it calls on
# every element; `map`, `filter`, `min` and `max` take a callable too and are left out.
_PURE_CALLS = _FRESH_CALLS | frozenset(
    {
        "list",
        "dict",
        "tuple",
        "enumerate",
        "zip",
        "isinstance",
        "any",
        "all",
        "sum",
        "reversed",
    }
)
# Module functions that read paths only. The real `build_k8s_dep_map` reaches the first two
# with `playbook_dir`, so without them no change to it could discharge.
_PURE_DOTTED = frozenset(
    {"os.path.join", "os.listdir", "os.path.exists", "os.path.isdir", "os.path.isfile"}
)
# Methods that read their receiver. A call to one counts as a read on any receiver but an
# imported module, whose function of the same name (`shutil.copy`) may write.
_READ_METHODS = frozenset(
    {
        "get", "keys", "values", "items", "copy", "count", "index", "startswith", "endswith",
        "split", "strip", "lower", "upper", "format", "join", "replace", "read",
    }
)  # fmt: skip


def _without_docstring(node):
    body = getattr(node, "body", None)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        node.body = body[1:]


def _binds(stmt: ast.stmt) -> list[str]:
    """The names a top-level statement binds, or none when it binds anything but plain names."""
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [stmt.name]
    if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
        targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
        names = [t.id for t in targets if isinstance(t, ast.Name)]
        return names if len(names) == len(targets) else []
    if isinstance(stmt, (ast.Import, ast.ImportFrom)):
        return [(a.asname or a.name).split(".")[0] for a in stmt.names]
    return []


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
        names = _binds(stmt)
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


_Body = ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda


def _bodies(node: ast.AST) -> list[_Body]:
    """The functions and lambdas in `node` that no other function or lambda in it encloses."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        return [node]
    return [b for child in ast.iter_child_nodes(node) for b in _bodies(child)]


class _Module:
    """What the write check needs to know about a plugin's top-level names.

    Attributes:
        names: every name a top-level statement binds.
        imports: the names an import binds.
        callables: name -> the bodies a call to it can run, for a def, a class (its methods)
          and a name bound to a lambda. A call to any other top-level name is opaque.
    """

    def __init__(self, tree: ast.Module):
        self.names: set[str] = set()
        self.imports: set[str] = set()
        self.callables: dict[str, list[_Body]] = {}
        for stmt in tree.body:
            names = _binds(stmt)
            self.names.update(names)
            if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                self.imports.update(names)
            elif isinstance(
                stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                self.callables[stmt.name] = _bodies(stmt)
            elif isinstance(stmt, (ast.Assign, ast.AnnAssign)) and isinstance(
                stmt.value, ast.Lambda
            ):
                self.callables.update((n, [stmt.value]) for n in names)


def _dotted(node) -> str | None:
    """`os.path.join` for an attribute chain over a plain name, else None."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    return ".".join([node.id, *reversed(parts)]) if isinstance(node, ast.Name) else None


def _opens_to_read(call: ast.Call) -> bool:
    """Whether an `open()` call's mode is absent or a literal that cannot write."""
    if any(k.arg is None for k in call.keywords):
        return False
    modes = [*call.args[1:2], *(k.value for k in call.keywords if k.arg == "mode")]
    return all(
        isinstance(m, ast.Constant)
        and isinstance(m.value, str)
        and not set(m.value) & set("wax+")
        for m in modes
    )


def _writes_outside_itself(fn: _Body, module: _Module, seen: set[str]) -> bool:
    """Whether `fn` may write to an object it did not create: an argument or a global.

    Taint starts at the parameters of `fn` and of every function nested in it, and at every
    top-level name, and flows through every assignment, loop and comprehension whose source
    names a tainted value, unless a `_FRESH_CALLS` call wraps it. A write is any of these:
    - an item or attribute assignment, a `del`, an augmented assignment to a tainted name or
      a `_MUTATORS` call, each allowed only on a clean name `fn` binds itself;
    - `global`, `nonlocal`, `setattr`, `delattr` or a dunder method call, outright;
    - a call that hands a tainted value, as an argument or as its receiver, to a callee this
      cannot read (#4842). It reads `_PURE_CALLS`, `_PURE_DOTTED`, a read-mode `open()`,
      `_READ_METHODS`, a function nested in `fn`, and the module's own `callables`.
    A top-level callable `fn` names, called or passed, is checked the same way, once per name
    in `seen`, so a helper it calls or hands to another call cannot write either.
    """
    params = {
        a.arg
        for args in ast.walk(fn)
        if isinstance(args, ast.arguments)
        for a in [
            *args.posonlyargs,
            *args.args,
            *args.kwonlyargs,
            args.vararg,
            args.kwarg,
        ]
        if a is not None
    }
    nested = {
        n.name
        for n in ast.walk(fn)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n is not fn
    }
    tainted = params | module.names
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
    local = bound | params | nested

    def clean_local(node) -> bool:
        root = _root(node)
        return root is not None and root in bound and root not in tainted

    def callee_writes(call: ast.Call) -> bool:
        func = call.func
        if isinstance(func, ast.Attribute) and func.attr.startswith("__"):
            return True
        if isinstance(func, ast.Attribute) and func.attr in _MUTATORS:
            return not clean_local(func.value)
        inputs = [*call.args, *(k.value for k in call.keywords)]
        if not isinstance(func, ast.Name):
            inputs.append(func.value if isinstance(func, ast.Attribute) else func)
        if not any(taints(v) for v in inputs):
            return False
        if isinstance(func, ast.Name):
            if func.id in nested or (
                func.id in module.callables and func.id not in local
            ):
                return False
            if func.id in local or func.id in module.names:
                return True
            if func.id == "sorted":
                return any(k.arg in (None, "key") for k in call.keywords)
            if func.id == "open":
                return not _opens_to_read(call)
            return func.id not in _PURE_CALLS
        if isinstance(func, ast.Attribute):
            if func.attr in _READ_METHODS and _root(func.value) not in module.imports:
                return False
            dotted = _dotted(func)
            return not (dotted in _PURE_DOTTED and dotted.split(".")[0] not in local)
        return True

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
            if callee_writes(node):
                return True
        if (
            isinstance(node, ast.Name)
            and node.id in module.callables
            and node.id not in local
            and node.id not in seen
        ):
            seen.add(node.id)
            if any(
                _writes_outside_itself(body, module, seen)
                for body in module.callables[node.id]
            ):
                return True
    return False


def order_only(path: str, before: str, after: str) -> bool:
    """Whether a filter plugin's change is confined to its `ORDER_ONLY_FILTERS` (#4781).

    True when every top-level name whose definition changed is reached by none of the
    plugin's other filters, at either end, `FilterModule` itself is unchanged, and no function
    or lambda in a changed top-level statement writes to an object it did not create
    (`_writes_outside_itself`). False on any doubt: a plugin not in the table, a parse error,
    a changed statement that binds no name, or a registration this cannot read.
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
    tree = ast.parse(after)
    module = _Module(tree)
    return not any(
        _writes_outside_itself(body, module, set())
        for stmt in tree.body
        if set(_binds(stmt)) & changed
        for body in _bodies(stmt)
    )
