"""The rules a red test is held to: why it failed, what it suppresses, what a fix may change.

WHY IT FAILED (#4023). `red_by_absence` sorts each red test's failure on the unchanged code
into an assertion or a name that is not there yet. The red gate accepts any `FAILED`. `red_prompt` tells the red author to reach a
not-yet-existing module inside the test body, so `assert import_module("new").f() is not None`
fails at the gate on the import and passes after any implementation of `f`. That test proves
the symbol was missing, not that its assertion can fail. The gate records which nodes failed
that way and refuses nothing: a refusal discards the whole red phase, and no production run
exists yet to calibrate one against. The reviewer is told which nodes to check for vacuity.

The failure text is the part of pytest's short summary after ` - `. It is cut to the terminal
width unless the run is `-vv`, which is how the red gate runs it.

WHAT IT SUPPRESSES (#4439). The red author commits with a plain `git commit`, so the `ty` hook
checks the red tree, and ty reports an import of a module that does not exist yet as an
`unresolved-import` error. A `# ty: ignore[unresolved-import]` passes that commit. Once the fix
adds the module, ty reports the same comment as an `unused-ignore-comment` warning, the hook
fails on it, and the fix commits nothing: the red file is not the fix's to edit, and in #4403
the auto-mode classifier denied removing the comments as a CI bypass. `stale_suppressions`
names each such comment the red commits add, so the red gate refuses them before any fix
exists. It counts a suppression that names no rule, in either the `ty:` or the `type:`
spelling, and one naming a rule in `ABSENCE_RULES`, the rules a fix resolves by adding a name.
`red_prompt` says how to reach a missing name without one.

WHAT A FIX MAY CHANGE (#4214, #4517). `forbidden_change` says what the fix changed in a red
test file that it may not. The green gate used to refuse any change to a red file. In #4213 the
fix round appended the test a reviewer had asked for, changed no red node, and the batch ended
unlanded. Hashing each red node instead would have let #4183's first fix through: it edited a
pre-existing function the red node calls, which changes what the node checks without touching
it.

THE RULE. A top-level statement is locked when the red commits wrote it, when a red node is
collected from it, or when a fix could not append it under the rule below, such as an
assignment that is not a constant, a class, an autouse fixture or a hook. A function or import
that a locked statement names is locked too, and so on transitively. A name counts when the
locked statement reads it, binds it, takes it as a parameter, which is how a test requests a
fixture, or spells it in a string. Every locked statement stays as it was, in order, compared by
`ast.dump`, so formatting and comments may change and the module docstring may be rewritten.

A constant, a statement that binds plain names to literal data (`_constant`), is locked only
when a red test reads it itself: when a statement the red commits wrote, a red node's own
function, a locked statement that is not a function, or a locked constant names it.

A fixture that a `conftest.py` fixture on the file's path requests counts as named too, as does
whatever that one requests in turn, and whatever an autouse one requests (#4543). pytest
resolves a conftest fixture's requests from the test's module, so the red file's own fixture of
that name serves every red node that requests the conftest one.

Every other statement in the fix's version must be a test function, a helper function, a
fixture, an import or a constant, and it may not bind a name a locked statement names: a second
`def test_two` replaces the red node pytest collects, a new import can shadow the code under
test, and a new fixture can override one a red node requests. A constant may bind such a name
only when an unlocked constant bound it in the red version, and never a name a red test reads
itself or one of `LITERAL_CALLS`, whose rebinding would make a constant run the fix's code at
import. A fixture may not be `autouse` or take `name=`, and an xunit or `pytest_*` hook is
refused, since each runs around every test in the module.

DECIDED: a function or import that was in the file before the red commits, and that no red node
reaches, is the fix's to change or delete. The same test in a file the red commits never touched
was always the fix's, so the lock on it was an accident of where the red author appended. In
#3675's judged records the append-only rule refused two edits the fix itself forced, which this
rule lets through: the callers of a parameter the fix dropped (#4338), and a pre-existing test
the red tests contradicted, whose restore left CI red (#4470). The reviewer and CI check such a
test, as they check every test outside a red file.

DECIDED: a constant that only an older function's body names is the fix's to change (#4542). In
#4372 the red test called the guard's `scheduled_reapers`, which reads the module's `REAPERS`,
and the fix had to change `REAPERS`. No rule on the diff tells that from tampering with data an
older helper checks, and the gate accepts the hole because it always accepted it one file over,
in an imported helper module. An older function a red node calls stays locked, so #4183's true
positive, a weakened pre-existing test the red node calls, is still refused. Only literal data
counts, because any other module-level statement runs code at import around every red test.

A CONFTEST.PY (#4542). `forbidden_conftest_change` lets a fix only delete from a `conftest.py`:
a fixture that is not autouse and that no red file names or requests through the conftests, a
helper or constant nothing kept names, and an import only deleted statements used. In #4359 the
red test demanded the deletion of a fixture nothing requested.

WHAT IT DOES NOT SEE. Code that runs at import time inside an allowed statement: a decorator
argument, a default value, an annotation, or an import of a new module with side effects. A
fixture in the file that only a pytest builtin or a plugin requests: `tmp_path` requests
`tmp_path_factory`, so the file's own `tmp_path_factory` changes what a red node's `tmp_path`
returns. A fixture a conftest requests by a name built at runtime. The green gate still runs
every red node, and CI runs the whole suite on the pushed head.
"""

import ast
import io
import re
import tokenize
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

_FAILED = re.compile(r"^FAILED (\S+) - (.*)$", re.M)
# A test that fails because the code under test is missing, before any assertion runs.
ABSENCE = ("ImportError", "ModuleNotFoundError", "AttributeError", "NameError")
# A test that fails on what the code does.
ASSERTION = ("assert ", "AssertionError", "Failed: DID NOT RAISE")


def cause(text: str) -> str:
    """`absence`, `assertion` or `other` for one node's failure text."""
    if text.startswith(tuple(f"{name}:" for name in ABSENCE)):
        return "absence"
    if text.startswith(ASSERTION):
        return "assertion"
    return "other"


def red_by_absence(output: str, nodes: list[str]) -> list[str]:
    """The `nodes` that failed on a missing name in a `pytest -vv -rA` run's summary."""
    texts = {m.group(1): m.group(2) for m in _FAILED.finditer(output)}
    return [n for n in nodes if cause(texts.get(n, "")) == "absence"]


# A type-checker suppression inside one comment, with the rules its brackets name. ty honours
# mypy's `type: ignore` spelling as well as its own.
_SUPPRESSION = re.compile(r"\b(?:ty|type):\s*ignore\b(?:\[([^\]]*)\])?")
# ty's rules for a name that is not there yet, as `ty explain rule` lists them in 0.0.86. A fix
# that adds the name leaves a suppression of any of them unused.
ABSENCE_RULES = frozenset(
    {
        "unresolved-import",
        "unresolved-attribute",
        "unresolved-reference",
        "possibly-missing-import",
        "possibly-missing-attribute",
        "possibly-missing-submodule",
        "possibly-unresolved-reference",
    }
)


def stale_suppressions(base_src: str, red_src: str) -> list[str]:
    """The suppressions `red_src` adds that a fix would leave unused, each as `line N: comment`.

    A comment `base_src` already carries, as the same text, is not added, wherever it moved.
    A source that does not tokenize yields nothing: pytest refuses it at collection.
    """
    before = Counter(text for _, text in _suppressions(base_src))
    added = []
    for line, text in _suppressions(red_src):
        if before[text]:
            before[text] -= 1
        else:
            added.append(f"line {line}: `{text}`")
    return added


def _suppressions(src: str) -> list[tuple[int, str]]:
    """Each comment in `src` that suppresses every rule or one of `ABSENCE_RULES`.

    Comments come from `tokenize`, so the same text inside a string literal is not one.
    """
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except tokenize.TokenError, SyntaxError:
        return []
    found = []
    for token in tokens:
        if token.type != tokenize.COMMENT:
            continue
        for match in _SUPPRESSION.finditer(token.string):
            rules = match.group(1)
            if rules is None or ABSENCE_RULES & {r.strip() for r in rules.split(",")}:
                found.append((token.start[0], token.string.strip()))
                break
    return found


# Functions pytest calls around the tests of a module rather than as tests.
XUNIT_HOOKS = frozenset(
    {
        "setup_module",
        "teardown_module",
        "setup_function",
        "teardown_function",
        "setup",
        "teardown",
        "setUpModule",
        "tearDownModule",
    }
)
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
# Names pytest reads from a test module or a conftest, so binding one is never only data.
PYTEST_NAMES = frozenset(
    {"pytestmark", "pytest_plugins", "collect_ignore", "collect_ignore_glob"}
)
# The builtins a constant may call. A fix may not rebind one in a red file.
LITERAL_CALLS = frozenset({"frozenset", "set", "tuple", "list", "dict"})
_CONTAINERS = (ast.Tuple, ast.List, ast.Set, ast.Dict)
# What each conftest fixture requests by the name a test requests it by, and what every
# autouse one requests, which every test requests in turn.
_Fixtures = tuple[dict[str, set[str]], set[str]]


@dataclass(frozen=True)
class _Lock:
    """What a red file's lock holds; the module docstring says why each part is there.

    Attributes:
        stmts: the locked statements, in the red version's order.
        names: every name they name, and every conftest fixture those request.
        direct: the names the red tests read themselves, not through an older function.
        free: the names the red version's unlocked constants bind.
    """

    stmts: list[ast.stmt]
    names: set[str]
    direct: set[str]
    free: set[str]


def forbidden_change(
    base_src: str,
    red_src: str,
    head_src: str,
    red_names: Iterable[str] = (),
    conftests: Iterable[str] = (),
) -> str:
    """Why `head_src` changes what a red test stands on, or "" when it does not.

    Args:
        base_src: the file before the red commits, "" when they created it or it is unknown.
            With "", every statement counts as the red commits', so only appending is allowed.
        red_src: the file as the red commits left it.
        head_src: the same file at the implementer's HEAD.
        red_names: the top-level names the file's red nodes are collected from.
        conftests: the source of every `conftest.py` pytest loads for the file.
    """
    try:
        head = ast.parse(head_src)
    except SyntaxError as exc:
        return f"does not parse: {exc.msg} at line {exc.lineno}"
    lock = _locked(base_src, red_src, set(red_names), _fixtures(conftests))
    kept = lock.stmts
    matched = 0
    added = []
    for stmt in _body(head):
        if matched < len(kept) and ast.dump(stmt) == ast.dump(kept[matched]):
            matched += 1
        else:
            added.append(stmt)
    if matched < len(kept):
        return f"changes or removes `{_label(kept[matched])}`"
    used = lock.names | LITERAL_CALLS
    hoisted = _hoistable(kept)
    for stmt in added:
        problem = (
            _constant_problem(stmt, used, lock)
            if _constant(stmt)
            else _addition_problem(stmt, used, hoisted)
        )
        if problem:
            return f"adds `{_label(stmt)}`, which {problem}"
    return ""


def _locked(
    base_src: str, red_src: str, red_names: set[str], fixtures: _Fixtures
) -> _Lock:
    """The red version's lock; the module docstring says what it holds."""
    red = _body(ast.parse(red_src))
    try:
        before = Counter(ast.dump(s) for s in _body(ast.parse(base_src)))
    except SyntaxError:
        before = Counter()
    # What the red commits wrote, and what the file held that a fix could not append.
    fixed = set()
    for stmt in red:
        dump = ast.dump(stmt)
        if before[dump]:
            before[dump] -= 1
            if _constant(stmt) or not _addition_problem(stmt, set()):
                continue
        fixed.add(id(stmt))
    names, direct = set(red_names), set(red_names)
    while True:
        names |= _requested(names, fixtures)
        stmts = [
            s
            for s in red
            if id(s) in fixed or _binds(s) & (direct if _constant(s) else names)
        ]
        # An older function's body is not what a red test reads itself.
        reads = [
            s for s in stmts if id(s) in fixed or _constant(s) or _binds(s) & red_names
        ]
        grown = names | _named(stmts), direct | _named(reads)
        if grown == (names, direct):
            locked = {id(s) for s in stmts}
            free = [_binds(s) for s in red if _constant(s) and id(s) not in locked]
            return _Lock(stmts, names, direct, set().union(*free))
        names, direct = grown


def _constant(stmt: ast.stmt) -> bool:
    """Whether `stmt` binds plain names to literal data, so evaluating it runs no code.

    The value may hold constants, containers, operators and calls of `LITERAL_CALLS` on
    those, and no name, attribute or subscript. A dunder or a name in `PYTEST_NAMES` is
    never a constant's, since pytest reads it.
    """
    return (
        isinstance(stmt, ast.Assign)
        and all(
            isinstance(t, ast.Name)
            and not t.id.startswith("__")
            and t.id not in PYTEST_NAMES
            for t in stmt.targets
        )
        and _literal(stmt.value)
    )


def _literal(node: ast.expr) -> bool:
    if isinstance(node, ast.Call):
        return (
            isinstance(node.func, ast.Name)
            and node.func.id in LITERAL_CALLS
            and not node.keywords
            and all(_literal(arg) for arg in node.args)
        )
    kinds = (ast.Constant, *_CONTAINERS, ast.UnaryOp, ast.BinOp)
    children = [c for c in ast.iter_child_nodes(node) if isinstance(c, ast.expr)]
    return isinstance(node, kinds) and all(_literal(c) for c in children)


def _constant_problem(stmt: ast.stmt, used: set[str], lock: _Lock) -> str:
    """Why a constant the red version lacks could change how a red test runs, or ""."""
    for name in sorted(_binds(stmt)):
        if name in lock.direct or (name in used and name not in lock.free):
            return f"rebinds `{name}`"
    return ""


def _fixtures(sources: Iterable[str]) -> _Fixtures:
    """What the fixtures in `sources` request; a source that does not parse adds nothing."""
    requests: dict[str, set[str]] = {}
    autouse: set[str] = set()
    for src in sources:
        try:
            body = ast.parse(src).body
        except SyntaxError:
            continue
        for stmt in body:
            if not isinstance(stmt, _FUNCTIONS) or not (fixture := _fixture(stmt)):
                continue
            args = stmt.args
            wants = {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
            wants |= _spelled([stmt])
            requests.setdefault(fixture[0], set()).update(wants)
            if fixture[1]:
                autouse |= wants
    return requests, autouse


def _requested(names: set[str], fixtures: _Fixtures) -> set[str]:
    """Every fixture name the conftest fixtures `names` reach request, autouse ones' too."""
    requests, found = fixtures[0], set(fixtures[1])
    todo = [*names, *found]
    while todo:
        for wanted in requests.get(todo.pop(), set()) - found - names:
            found.add(wanted)
            todo.append(wanted)
    return found


def _fixture(stmt: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[str, bool] | None:
    """`(name, autouse)` for a `pytest.fixture` function, by the name a test requests."""
    for decorator in stmt.decorator_list:
        call = decorator if isinstance(decorator, ast.Call) else None
        if ast.unparse(call.func if call else decorator) != "pytest.fixture":
            continue
        keywords = {k.arg: k.value for k in call.keywords} if call else {}
        name, auto = keywords.get("name"), keywords.get("autouse")
        named = isinstance(name, ast.Constant) and isinstance(name.value, str)
        # An `autouse=` that is not a literal false counts as autouse.
        off = auto is None or (isinstance(auto, ast.Constant) and not auto.value)
        return (str(name.value) if named else stmt.name), not off
    return None


def forbidden_conftest_change(
    red_src: str, head_src: str, red_files: Iterable[str], conftests: Iterable[str]
) -> str:
    """Why `head_src` changes a `conftest.py` beyond what the module docstring allows, or "".

    Args:
        red_src: the conftest as the red commits left it.
        head_src: the same conftest at the implementer's HEAD.
        red_files: the source of every red test file as the red commits left it.
        conftests: the source of every `conftest.py` pytest loads for a red test file.
    """
    try:
        head = _body(ast.parse(head_src))
    except SyntaxError as exc:
        return f"does not parse: {exc.msg} at line {exc.lineno}"
    # Each red file parses: the red gate collected it.
    named = set().union(*(_named(ast.parse(src).body) for src in red_files))
    requested = named | _requested(named, _fixtures(conftests))
    kept, gone = [], []
    for stmt in _body(ast.parse(red_src)):
        if len(kept) < len(head) and ast.dump(stmt) == ast.dump(head[len(kept)]):
            kept.append(stmt)
        else:
            gone.append(stmt)
    if len(kept) < len(head):
        return f"adds or changes `{_label(head[len(kept)])}`"
    for stmt in gone:
        others = _named([s for s in gone if s is not stmt])
        problem = _deletion_problem(stmt, _named(kept) | requested, others)
        if problem:
            return f"deletes `{_label(stmt)}`, which {problem}"
    return ""


def _deletion_problem(stmt: ast.stmt, held: set[str], deleted: set[str]) -> str:
    """Why deleting `stmt` from a conftest could change how a red test runs, or "".

    Args:
        stmt: the deleted statement.
        held: every name the conftest's kept statements or a red file name, and every
            fixture those request.
        deleted: every name the other deleted statements name.
    """
    bound = _binds(stmt)
    imports = isinstance(stmt, (ast.Import, ast.ImportFrom))
    if isinstance(stmt, _FUNCTIONS) and (fixture := _fixture(stmt)):
        if fixture[1]:
            return "is an autouse fixture, which runs around every test"
        bound.add(fixture[0])
    elif (
        not imports
        and not _constant(stmt)
        and not (
            isinstance(stmt, _FUNCTIONS)
            and not stmt.decorator_list
            and stmt.name not in XUNIT_HOOKS
            and not stmt.name.startswith("pytest_")
        )
    ):
        return "is not a fixture, a plain helper, a constant or an import"
    if names := sorted(bound & held):
        return f"binds `{names[0]}`, which a red test or the conftest still uses"
    if imports and not bound <= deleted:
        return (
            "is an import no deleted statement used, so it may be there for its effect"
        )
    return ""


def _hoistable(kept: list[ast.stmt]) -> frozenset[tuple]:
    """The imports a locked function makes in its body, which the fix may repeat at the top.

    #4470's fix moved a red test's own `from lib.exit_codes import LAND_BAD_ARGS` to module
    level for the pre-existing test it updated. Inside the red test the local import still
    wins, so the copy changes nothing there. A name a locked statement binds at module level is
    left out, since the copy would rebind it.
    """
    top = set()
    for stmt in kept:
        if isinstance(stmt, (*_FUNCTIONS, ast.ClassDef)):
            top.add(stmt.name)
        elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
            top |= _binds(stmt)
        else:
            top |= _names([stmt])
    return frozenset(
        _import_key(node, alias)
        for stmt in kept
        if isinstance(stmt, _FUNCTIONS)
        for node in ast.walk(stmt)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
        if _bound(alias) not in top
    )


def _import_key(stmt: ast.Import | ast.ImportFrom, alias: ast.alias) -> tuple:
    """What one name of an import binds, as `_hoistable` compares it."""
    module = (stmt.module, stmt.level) if isinstance(stmt, ast.ImportFrom) else None
    return module, alias.name, alias.asname


def _binds(stmt: ast.stmt) -> set[str]:
    """The names a function, import or constant binds at module level."""
    if isinstance(stmt, (ast.Import, ast.ImportFrom)):
        return {_bound(alias) for alias in stmt.names}
    if isinstance(stmt, ast.Assign) and _constant(stmt):
        return {t.id for t in stmt.targets if isinstance(t, ast.Name)}
    return {stmt.name} if isinstance(stmt, _FUNCTIONS) else set()


def _named(stmts: list[ast.stmt]) -> set[str]:
    """Every name `stmts` name: what they bind, read or take, and what they spell."""
    return _names(stmts) | _spelled(stmts)


def _spelled(stmts: list[ast.stmt]) -> set[str]:
    """Every string constant in `stmts` that could be a name, as `getfixturevalue` takes."""
    return {
        node.value
        for stmt in stmts
        for node in ast.walk(stmt)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.isidentifier()
    }


def _body(module: ast.Module) -> list[ast.stmt]:
    """The module's statements without a leading docstring."""
    body = module.body
    first = body[0] if body else None
    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        return body[1:]
    return body


def _label(stmt: ast.stmt) -> str:
    if isinstance(stmt, (*_FUNCTIONS, ast.ClassDef)):
        kind = "class" if isinstance(stmt, ast.ClassDef) else "def"
        return f"{kind} {stmt.name}"
    return f"{type(stmt).__name__.lower()} at line {stmt.lineno}"


def _names(stmts: list[ast.stmt]) -> set[str]:
    """Every name `stmts` bind, read or take as a parameter, at any depth."""
    names = set()
    for node in (n for stmt in stmts for n in ast.walk(stmt)):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, (*_FUNCTIONS, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.alias):
            names.add(_bound(node))
    return names


def _bound(alias: ast.alias) -> str:
    """The name an import binds: `import a.b` binds `a`."""
    return alias.asname or alias.name.split(".")[0]


def _addition_problem(
    stmt: ast.stmt, used: set[str], hoisted: frozenset[tuple] = frozenset()
) -> str:
    """Why a statement the red version lacks could change how a red test runs, or "".

    Args:
        stmt: the statement.
        used: the names it may not bind.
        hoisted: the `_import_key`s of imports it may repeat at module level.
    """
    if isinstance(stmt, (ast.Import, ast.ImportFrom)):
        for alias in stmt.names:
            if alias.name == "*":
                return "is a star import"
            if _bound(alias) in used and _import_key(stmt, alias) not in hoisted:
                return f"rebinds `{_bound(alias)}`"
        return ""
    if not isinstance(stmt, _FUNCTIONS):
        return "is not allowed: only test functions, fixtures, helpers, imports and constants may be appended"
    if stmt.name in used:
        return f"rebinds `{stmt.name}`"
    if stmt.name in XUNIT_HOOKS or stmt.name.startswith("pytest_"):
        return "is a hook that runs around the tests"
    for decorator in stmt.decorator_list:
        problem = _decorator_problem(decorator)
        if problem:
            return problem
    return ""


def _decorator_problem(decorator: ast.expr) -> str:
    """Why a decorator on an appended function is refused; a mark or a plain fixture is not."""
    call = decorator if isinstance(decorator, ast.Call) else None
    target = ast.unparse(call.func if call else decorator)
    if target.startswith("pytest.mark."):
        return ""
    if target != "pytest.fixture":
        return f"carries the decorator `{target}`, which is neither a pytest mark nor a fixture"
    for keyword in call.keywords if call else ():
        if keyword.arg == "autouse":
            return "is an autouse fixture, which runs around every test"
        if keyword.arg == "name":
            return (
                "is a fixture with `name=`, which can override one a red test requests"
            )
    return ""
