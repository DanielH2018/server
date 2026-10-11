"""An AST scan of a filter plugin: which entry fields each registered filter reads.

`test_narrow_containers_fields.py` holds `FILTER_FIELDS` in `narrow_lib/containers.py` against
it. A row that omits a field its filter reads narrows a `containers_list` edit past a role
whose render that field moves, which is an under-deploy, so the scan errs toward reading more:
any use of an entry it cannot resolve to a literal key is reported as opaque, and an opaque
use the test has not vetted fails it. Imported by bare name, as `_narrow_fixtures.py` is.
"""

import ast

# What an expression may hold: a whole `containers_list` entry, or a list of entries. A field's
# value (`entry["port"]`, `entry.get("metrics")`) is neither. The field's name already covers
# every read beneath it, so the scan lets a field's value be walked freely.
_ENTRY, _LIST = "entry", "list"
_NONE: frozenset[str] = frozenset()
_BOTH = frozenset({_ENTRY, _LIST})
# Methods that store their argument in the receiver, which then holds a list of entries. Any
# other method handed an entry may have copied its fields, so its receiver may hold either.
_COLLECTS = frozenset({"append", "add", "insert"})


def filter_reads(source: str) -> dict[str, tuple[set[str], set[str]]]:
    """Each registered filter's string keys and its opaque uses, through the helpers it calls.

    A key is a constant in `x.get("k")`, `x["k"]` or `"k" in x`. That is a superset of the
    entry fields (a nested dict's keys come along), which errs toward more readers. An opaque
    use is any use of an entry the scan cannot resolve to a literal key. `_Frame` lists the
    uses it can resolve and treats everything else as opaque, so a shape nobody anticipated
    fails the guard rather than slipping past it.
    """
    tree = ast.parse(source)
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    registered = {
        key.value: value.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Dict)
        for key, value in zip(node.keys, node.values, strict=True)
        if isinstance(key, ast.Constant)
        and isinstance(key.value, str)
        and isinstance(value, ast.Name)
        and value.id in funcs
    }
    scan = _Scan(funcs)
    reads: dict[str, tuple[set[str], set[str]]] = {}
    for name, func in registered.items():
        # Jinja hands the piped value, `containers_list`, to the filter's first parameter.
        params = _params(funcs[func])[1]
        kinds = tuple(
            frozenset({_LIST}) if i == 0 else _NONE for i in range(len(params))
        )
        keys, opaque, _ = scan.function(func, kinds, is_filter=True)
        reads[name] = ({k for k in keys if isinstance(k, str)}, opaque)
    return reads


def _params(fn: ast.FunctionDef) -> tuple[list[str], list[str]]:
    """The parameters a call can fill by position, and every one it can fill by name."""
    positional = [a.arg for a in fn.args.posonlyargs + fn.args.args]
    return positional, positional + [a.arg for a in fn.args.kwonlyargs]


class _Scan:
    """Scans each module function once per combination of entry-holding arguments."""

    def __init__(self, funcs: dict[str, ast.FunctionDef]):
        self.funcs = funcs
        self.memo: dict[tuple, tuple[set[object], set[str], frozenset[str]]] = {}

    def function(
        self, name: str, kinds: tuple[frozenset[str], ...], is_filter: bool = False
    ) -> tuple[set[object], set[str], frozenset[str]]:
        """`name`'s keys, opaque uses and what its returns may hold, given what each arg holds."""
        memo_key = (name, kinds, is_filter)
        if memo_key not in self.memo:
            # A recursive call sees the most conservative answer while the scan runs.
            self.memo[memo_key] = (set(), set(), _BOTH)
            frame = _Frame(self, self.funcs[name], kinds, is_filter)
            self.memo[memo_key] = frame.run()
        return self.memo[memo_key]


class _Frame:
    """One function body, read for what each name may hold and how each entry is used.

    An entry may be read by a literal key (`e["k"]`, `e.get("k")`, `"k" in e`), tested
    (`isinstance(e, ...)`, `e is None`, `not e`), handed to a module helper (scanned the same
    way) or returned from a helper. A list of entries may be iterated, indexed and handed to a
    helper. Every other use is opaque: a computed key, iterating an entry, `**e`, an f-string
    of it, handing it to any other call or method, storing it in a container. A filter
    returning an entry is opaque too, since the template then reads whatever it likes.
    The reading is flow-insensitive: a name holds everything any assignment gives it.
    """

    def __init__(self, scan: _Scan, fn: ast.FunctionDef, kinds, is_filter: bool):
        self.scan, self.fn, self.is_filter = scan, fn, is_filter
        self.holds: dict[str, frozenset[str]] = dict(
            zip(_params(fn)[1], kinds, strict=True)
        )
        self.keys: set[object] = set()
        self.opaque: set[str] = set()
        self.ret = _NONE

    def run(self) -> tuple[set[object], set[str], frozenset[str]]:
        # Names only gain kinds, so this reaches a fixpoint within one pass per name.
        for _ in range(sum(1 for _ in ast.walk(self.fn)) + 1):
            before = dict(self.holds)
            self.keys, self.opaque, self.ret = set(), set(), _NONE
            self.block(self.fn.body)
            if self.holds == before:
                break
        for node in ast.walk(self.fn):
            self.keys |= _constant_keys(node)
        return self.keys, self.opaque, self.ret

    def flag(self, label: str) -> None:
        self.opaque.add(label)

    def bind(self, target: ast.expr, kinds: frozenset[str]) -> None:
        if isinstance(target, ast.Name):
            self.holds[target.id] = self.holds.get(target.id, _NONE) | kinds
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                self.bind(elt.value if isinstance(elt, ast.Starred) else elt, kinds)
        elif isinstance(target, ast.Subscript):
            self.expr(target.value)
            if self.expr(target.slice) or kinds:
                self.flag(f"{ast.unparse(target)} = ...")
        else:
            self.expr(target)
            if kinds:
                self.flag(f"{ast.unparse(target)} = ...")

    def block(self, stmts: list[ast.stmt]) -> None:
        for s in stmts:
            self.stmt(s)

    def stmt(self, s: ast.stmt) -> None:
        if isinstance(s, ast.Assign):
            kinds = self.expr(s.value)
            for target in s.targets:
                self.bind(target, kinds)
        elif isinstance(s, ast.AnnAssign):
            self.bind(s.target, self.expr(s.value))
        elif isinstance(s, ast.AugAssign):
            kinds = self.expr(s.value)
            if kinds:
                self.flag(ast.unparse(s))
            self.bind(s.target, kinds)
        elif isinstance(s, (ast.For, ast.AsyncFor)):
            self.bind(s.target, self.iterate(s.iter, f"for over {ast.unparse(s.iter)}"))
            self.block(s.body + s.orelse)
        elif isinstance(s, (ast.If, ast.While)):
            self.expr(s.test)  # truthiness reads no field
            self.block(s.body + s.orelse)
        elif isinstance(s, ast.Return):
            kinds = self.expr(s.value)
            if self.is_filter and kinds and s.value is not None:
                self.flag(f"return {ast.unparse(s.value)}")
            self.ret |= kinds
        elif isinstance(s, (ast.Expr, ast.Raise, ast.Assert)):
            for child in ast.iter_child_nodes(s):
                self.expr(child)  # the statement's own value goes nowhere
        elif isinstance(s, (ast.With, ast.AsyncWith)):
            for item in s.items:
                kinds = self.expr(item.context_expr)
                if item.optional_vars is not None:
                    self.bind(item.optional_vars, kinds)
            self.block(s.body)
        elif isinstance(s, (ast.Try, ast.TryStar)):
            self.block(s.body + s.orelse + s.finalbody)
            for handler in s.handlers:
                self.block(handler.body)
        elif isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            self.block(
                s.body
            )  # a closure's reads count against the function holding it
        elif not isinstance(s, _INERT) and any(
            isinstance(n, ast.Name) and self.holds.get(n.id) for n in ast.walk(s)
        ):
            self.flag(type(s).__name__)  # `match` and anything newer: not followed

    def iterate(self, it: ast.expr, label: str) -> frozenset[str]:
        """What a loop over `it` binds: an entry from a list of them. Walking an entry is opaque."""
        kinds = self.expr(it)
        if _ENTRY in kinds:
            self.flag(label)
        return frozenset({_ENTRY}) if _LIST in kinds else _NONE

    def generators(self, gens: list[ast.comprehension]) -> None:
        for gen in gens:
            self.bind(
                gen.target, self.iterate(gen.iter, f"for over {ast.unparse(gen.iter)}")
            )
            for cond in gen.ifs:
                self.expr(cond)

    def expr(self, node: ast.AST | None) -> frozenset[str]:
        """What `node` may hold, flagging every entry use inside it the scan cannot resolve."""
        if node is None or isinstance(node, ast.Constant):
            return _NONE
        if isinstance(node, ast.Name):
            return self.holds.get(node.id, _NONE)
        if isinstance(node, ast.Subscript):
            base = self.expr(node.value)
            computed = not isinstance(node.slice, ast.Constant)
            if self.expr(node.slice) or (_ENTRY in base and computed):
                self.flag(ast.unparse(node))
            return frozenset({_ENTRY}) if _LIST in base else _NONE
        if isinstance(node, ast.BoolOp):
            return frozenset().union(*[self.expr(v) for v in node.values])
        if isinstance(node, ast.IfExp):
            self.expr(node.test)
            return self.expr(node.body) | self.expr(node.orelse)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            self.expr(node.operand)
            return _NONE
        if isinstance(node, ast.Compare):
            return self.compare(node)
        if isinstance(node, ast.Call):
            return self.call(node)
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            held = _NONE
            for elt in node.elts:
                if isinstance(elt, ast.Starred):
                    held |= self.iterate(elt.value, ast.unparse(elt))
                else:
                    held |= self.expr(elt)
            return frozenset({_LIST}) if held else _NONE
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
            self.generators(node.generators)
            return frozenset({_LIST}) if self.expr(node.elt) else _NONE
        if isinstance(node, ast.DictComp):
            self.generators(node.generators)
            if self.expr(node.key) | self.expr(node.value):
                self.flag(ast.unparse(node))
            return _NONE
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                held = self.expr(key) | self.expr(value)
                if held and key is None:
                    self.flag("**")
                elif held and key is not None:
                    self.flag(f"{{{ast.unparse(key)}: {ast.unparse(value)}}}")
            return _NONE
        if isinstance(node, ast.JoinedStr):
            for value in node.values:
                if isinstance(value, ast.FormattedValue):
                    self.expr(value.format_spec)
                    if self.expr(value.value):
                        self.flag(f"f-string {{{ast.unparse(value.value)}}}")
            return _NONE
        if isinstance(node, ast.NamedExpr):
            kinds = self.expr(node.value)
            self.bind(node.target, kinds)
            return kinds
        if isinstance(node, ast.Attribute):
            if self.expr(node.value):
                self.flag(f".{node.attr}")
            return _NONE
        held = [
            self.expr(c) for c in ast.iter_child_nodes(node) if isinstance(c, ast.expr)
        ]
        if any(held):
            self.flag(ast.unparse(node))
        return _NONE

    def compare(self, node: ast.Compare) -> frozenset[str]:
        operands = [node.left, *node.comparators]
        kinds = [self.expr(o) for o in operands]
        for i, op in enumerate(node.ops):
            if isinstance(op, (ast.Is, ast.IsNot)):
                continue  # identity reads no field
            if isinstance(op, (ast.In, ast.NotIn)) and isinstance(
                operands[i], ast.Constant
            ):
                continue  # `"k" in e` tests a literal key
            if kinds[i] or kinds[i + 1]:
                self.flag(ast.unparse(node))
        return _NONE

    def call(self, node: ast.Call) -> frozenset[str]:
        func = node.func
        if isinstance(func, ast.Name) and func.id in self.scan.funcs:
            return self.helper(node, self.scan.funcs[func.id])
        args = list(node.args)
        if isinstance(func, ast.Name) and func.id == "isinstance" and args:
            self.expr(args.pop(0))  # a type test reads no field
        default = _NONE
        if isinstance(func, ast.Attribute):
            receiver = self.expr(func.value)
            if func.attr == "get" and _ENTRY in receiver:
                if args and isinstance(args[0], ast.Constant):
                    args.pop(0)
                    if args:
                        default = self.expr(args.pop(0))  # `e.get("k", fallback)`
                else:
                    self.flag(".get(<computed>)")
            elif receiver and not (receiver == {_LIST} and func.attr in _COLLECTS):
                # Adding to a list of entries reads none of them; any other method may.
                self.flag(f".{func.attr}()")
        else:
            self.expr(func)
        handed = _NONE
        for arg in args:
            handed |= self.expr(arg.value if isinstance(arg, ast.Starred) else arg)
        for kw in node.keywords:
            kinds = self.expr(kw.value)
            if kinds and kw.arg is None:
                self.flag("**")
            handed |= kinds
        if not handed:
            return default
        self.flag(f"{ast.unparse(func)}()")
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            self.bind(
                func.value, frozenset({_LIST}) if func.attr in _COLLECTS else _BOTH
            )
        return _BOTH

    def helper(self, node: ast.Call, fn: ast.FunctionDef) -> frozenset[str]:
        """Scan a module helper with what each of its arguments holds; its result is its return."""
        positional, named = _params(fn)
        kinds = dict.fromkeys(named, _NONE)
        for i, arg in enumerate(node.args):
            held = self.expr(arg.value if isinstance(arg, ast.Starred) else arg)
            if i < len(positional) and not isinstance(arg, ast.Starred):
                kinds[positional[i]] = held
            elif held:
                self.flag(f"{ast.unparse(node.func)}(*...)")
        for kw in node.keywords:
            held = self.expr(kw.value)
            if kw.arg in kinds:
                kinds[kw.arg] = held
            elif held:
                self.flag(f"{ast.unparse(node.func)}(**...)")
        keys, opaque, ret = self.scan.function(fn.name, tuple(kinds[p] for p in named))
        self.keys |= keys
        self.opaque |= opaque
        return ret


# Statements that hold no expression an entry could reach.
_INERT = (
    ast.Pass,
    ast.Break,
    ast.Continue,
    ast.Global,
    ast.Nonlocal,
    ast.Delete,
    ast.Import,
    ast.ImportFrom,
)


def _constant_keys(node: ast.AST) -> set[object]:
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    ):
        return {node.args[0].value}
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
        return {node.slice.value}
    if (
        isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Constant)
        and any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops)
    ):
        return {node.left.value}
    return set()
