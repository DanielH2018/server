"""The monkeypatch census's counter: which patches in a test module target a first-party module.

`_ratchet.py` holds the allowlist comparisons both ratchets share; this module holds what only
the monkeypatch ratchet needs, the AST reading that turns a test module into a count. The
census that runs it over the tree is `_ratchet_census.py`, and its tests are
`ansible/tests/repo/test_ratchet_rules.py`.

What the heuristic counts, and what it misses:

- Counted: `monkeypatch.setattr(<name>, ...)` or `...(<a>.<b>, ...)` where the root name is
  bound to a FIRST-PARTY module — one whose name matches a tracked `.py` file or the
  directory that directly holds one. `sys`, `subprocess` and `urllib` are not counted: no
  seam can remove a patch on the standard library, so those entries could never reach zero.
- Counted: a name assigned from `importlib.util.module_from_spec(...)` or
  `importlib.import_module(...)`. That is how the hook and cluster-side tests reach a module
  whose filename is not an identifier (`session-health.py`); 57 patches across five files
  were invisible while only `import` statements were read.
- Counted: a parameter named after a conftest fixture that hands back a module. A fixture is
  read as module-returning when it is annotated `-> ModuleType`, or when it imports a name and
  returns it. `gitops_deploy/tests/conftest.py` does both, and three of its sibling test
  modules patched the deployer through that argument while counting 0 — five patches in
  `test_gitops_deploy_alert_channels.py`, four and two in the other two. The caller collects the fixture names from
  every `conftest.py` on a test's directory chain, the way pytest resolves one.
- Not counted: a fixture that returns a module with neither the annotation nor a bare
  `import`/`return` of the same name — a factory closure, say. The annotation is the signal
  the repo already writes; a dataflow analysis across a conftest's own imports is not.
- Counted: the string-target form `monkeypatch.setattr("<mod>.<attr>", ...)`, when the first
  dotted segment is a first-party module name. It is the object form's equal at runtime and
  pins the same module name into the test, so counting one and not the other selected a
  spelling rather than a design: `scripts/dev/tests/test_prune_worktrees.py` patched
  first-party internals fourteen times and measured 0, and on 2026-09-05 an implementer chose
  the string form there because it was the form the ratchet did not see. The string carries
  the module's own dotted name rather than a local alias, so its root is matched against the
  first-party names directly and not against what the test imported. `"subprocess.run"` stays
  out for the same reason a patch on `sys` does.
- Counted: an assignment whose target is an attribute chain rooted at a first-party module
  name, `mod.attr = double` or `mod.sub.attr = double`, plain, augmented, annotated or inside a
  tuple target. It does what `monkeypatch.setattr` does and pins the same name into the test,
  with less safety: an exception between the save and the `try` leaks the double into later
  tests (#3670). Four such patches were hiding behind ty suppressions.
  An assignment inside a `finally:` block is not counted, because that is the restore half of
  the same patch. The root is resolved the way the object form of `monkeypatch.setattr` is,
  so `cfg.X = 1` on a local object and `sys.argv = []` on the standard library stay out.
- Not counted: a receiver spelled anything but `monkeypatch`, a `setattr(mod, "x", v)` call,
  and `delattr`/`setitem`/`setenv`/`chdir`. A restore written outside a `finally:` block
  counts as a second patch.
- Not counted: a patch on an imported class or function, unless its name happens to match a
  first-party module name. Restricting roots to module names is what keeps the standard
  library out, and an import statement does not say which kind of object it binds.
- Over-counted: `import_module("json")` would count, because the argument is not resolved.
  No test in this tree does that.
- Per file, so moving patches from a listed test module into a new one lowers one entry and
  adds another while removing no patching. That is inherent in the one-line-per-file format;
  a `# TOTAL n` line would conflict on every parallel PR. The added entry shows in the diff.
"""

import ast
from collections.abc import Collection, Iterable

# `mod = importlib.util.module_from_spec(spec)` and `mod = importlib.import_module("x")` both
# bind a module to a plain name, which no import statement records.
DYNAMIC_IMPORTS = frozenset({"module_from_spec", "import_module"})

# What a conftest fixture writes when it hands a test the module itself.
MODULE_ANNOTATIONS = frozenset({"ModuleType", "types.ModuleType"})


def _returns_a_module(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether this function hands back a module, by its annotation or by its body.

    Two spellings, because a conftest writes either: `-> ModuleType`, or an `import x` inside
    the fixture followed by `return x` (which is how a fixture defers an import that must not
    run at collection).
    """
    if fn.returns is not None and ast.unparse(fn.returns) in MODULE_ANNOTATIONS:
        return True
    imported = {
        alias.asname or alias.name.split(".")[0]
        for node in ast.walk(fn)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    return any(
        isinstance(node, ast.Return)
        and isinstance(node.value, ast.Name)
        and node.value.id in imported
        for node in ast.walk(fn)
    )


def module_fixture_names(source: str) -> frozenset[str]:
    """Every top-level `@pytest.fixture` in `source` that hands back a module.

    A test module never imports these — it names one as a parameter and pytest passes the
    module in, so `_bound_module_names` has no import statement to read. The caller unions
    this over every `conftest.py` on a test's directory chain, the way pytest resolves one.
    """
    return frozenset(
        node.name
        for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and any(
            ast.unparse(dec).split("(")[0].endswith("fixture")
            for dec in node.decorator_list
        )
        and _returns_a_module(node)
    )


def first_party_module_names(tracked: Iterable[str]) -> frozenset[str]:
    """Every bare name an `import` in this repo could bind to a module of this repo.

    A module's own stem, plus the directory that directly holds it — that directory is what a
    dotted `bridge.config` import names. Directories further up (`ansible`, `roles`) are left
    out: nothing imports them, and `ansible` is a third-party package.
    """
    names: set[str] = set()
    for rel in tracked:
        *parents, name = rel.split("/")
        names.add(name.removesuffix(".py"))
        if parents:
            names.add(parents[-1])
    return frozenset(names)


def _bound_module_names(
    tree: ast.AST,
    first_party: Collection[str],
    module_fixtures: Collection[str] = (),
) -> set[str]:
    """The local names in `tree` that hold a first-party module.

    `module_fixtures` are conftest fixture names that hand back a module; a function parameter
    spelled with one of them holds that module without any import saying so.
    """
    bound: set[str] = set()
    for node in ast.walk(tree):
        match node:
            case ast.FunctionDef(args=args) | ast.AsyncFunctionDef(args=args):
                bound |= {
                    arg.arg
                    for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs)
                    if arg.arg in module_fixtures
                }
            case ast.Import(names=aliases):
                for alias in aliases:
                    head, tail = alias.name.split(".")[0], alias.name.split(".")[-1]
                    # `import a.b` binds `a`; `import a.b as m` binds the module `a.b`.
                    local, target = (
                        (alias.asname, tail) if alias.asname else (head, head)
                    )
                    if target in first_party:
                        bound.add(local)
            case ast.ImportFrom(names=aliases):
                bound |= {
                    alias.asname or alias.name
                    for alias in aliases
                    if alias.name in first_party
                }
            case ast.Assign(
                targets=targets, value=ast.Call(func=ast.Attribute(attr=attr))
            ) if attr in DYNAMIC_IMPORTS:
                bound |= {t.id for t in targets if isinstance(t, ast.Name)}
    return bound


def _targets_a_first_party_module(
    target: ast.expr,
    bound: Collection[str],
    first_party: Collection[str],
) -> bool:
    """Whether one `monkeypatch.setattr` target names a first-party module.

    Two spellings, identical at runtime, resolved against different sets. The object form
    walks the attribute chain down to its root name and asks whether an import bound that
    name to a first-party module. The string form carries the module's own dotted name, so
    there is no local binding to read and its root segment is matched against the first-party
    names themselves.
    """
    # DECIDED: the string form counts, resolved against `first_party` rather than `bound`. It
    # is the object form's equal at runtime, and counting only the object form taught authors
    # which spelling the guard could not see. The rationale and the incident are in this
    # module's docstring.
    if isinstance(target, ast.Constant) and isinstance(target.value, str):
        root, _, attr = target.value.partition(".")
        return bool(attr) and root in first_party
    while isinstance(target, ast.Attribute):
        target = target.value
    return isinstance(target, ast.Name) and target.id in bound


def _assigned_attributes(node: ast.AST) -> list[ast.Attribute]:
    """The attribute targets one assignment statement writes, tuple targets unpacked."""
    pending: list[ast.expr]
    match node:
        case ast.Assign(targets=targets):
            pending = list(targets)
        case ast.AugAssign(target=target):
            pending = [target]
        case ast.AnnAssign(target=target, value=ast.expr()):
            pending = [target]
        case _:
            return []
    found: list[ast.Attribute] = []
    while pending:
        target = pending.pop()
        match target:
            case ast.Attribute():
                found.append(target)
            case ast.Tuple(elts=elts) | ast.List(elts=elts):
                pending.extend(elts)
            case ast.Starred(value=value):
                pending.append(value)
    return found


def _restore_nodes(tree: ast.AST) -> set[int]:
    """The `id` of every node inside a `finally:` block.

    A hand-rolled patch saves the attribute, assigns a double and puts the original back in
    `finally`. The restore is an assignment to the same module attribute, so counting it would
    score one patch as two where `monkeypatch.setattr` scores it as one.
    """
    return {
        id(inner)
        for node in ast.walk(tree)
        if isinstance(node, ast.Try | ast.TryStar)
        for stmt in node.finalbody
        for inner in ast.walk(stmt)
    }


def count_module_patches(
    source: str,
    first_party: Collection[str],
    module_fixtures: Collection[str] = (),
) -> int:
    """How many patches in `source` target a first-party module.

    A patch is a `monkeypatch.setattr` call, or an assignment to an attribute of a name bound
    to a first-party module (`mod.attr = double`, `mod.sub.attr = double`). `module_fixtures`
    names the conftest fixtures that hand back a module, so a patch on such a parameter
    counts. The module docstring lists what this deliberately does not see.
    """
    tree = ast.parse(source)
    bound = _bound_module_names(tree, first_party, module_fixtures)
    restores = _restore_nodes(tree)

    counted = 0
    for node in ast.walk(tree):
        match node:
            case ast.Call(
                func=ast.Attribute(value=ast.Name(id="monkeypatch"), attr="setattr"),
                args=[target, *_],
            ):
                if _targets_a_first_party_module(target, bound, first_party):
                    counted += 1
        if id(node) not in restores:
            counted += sum(
                _targets_a_first_party_module(attribute, bound, first_party)
                for attribute in _assigned_attributes(node)
            )
    return counted
