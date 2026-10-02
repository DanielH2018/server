"""The AST detector behind the render gate: which expressions read a template's SOURCE text.

`test_guard_tests_read_renders_not_templates.py` holds the rule — the directories it scans,
the exempt modules and the tests. This module holds the pure half, so the rule's lists and its
red proofs stay inside the 500-line test cap while the detector grows.

A read is spelled four ways, and three of them hide the template path from the read site:

* **At the read site.** `(ROLES / "x" / "templates" / "y.j2").read_text()` names the path in
  the call itself.
* **Through a binding.** A name an assignment, a `for` or a comprehension bound to a template
  path. `_bindings` resolves these to a fixed point, because one binding feeds another.
* **Through a wrapper.** `sorted(tdir.glob("*.j2"))` puts a `sorted` call where `_bindings`
  looked for a `glob`, so the loop target never bound and the read dropped out of the census
  (#3209, #3211). `_unwrap` looks through the wrapping call.
* **Through a helper's parameter.** `def f(p): return p.read_text()` called as `f(tpl)` leaves
  no template path at the read site at all (#3209). `_reading_helpers` finds the functions
  THIS module defines whose parameter is read as a file, and the call to one counts as the read.
  Only a locally defined function qualifies: `_k8s_render.render_role_template("prowlarr",
  "x.yaml.j2", {})` reads a path it builds from its `name` argument, so resolving imported
  functions would flag every sanctioned render call as a source read.
* **Through an import.** A template path bound in one module and read in another escaped the
  census entirely (#3210): `from _fence_probe import ORCHESTRATOR` followed by
  `ORCHESTRATOR.read_text()`. `imported_template_names` resolves a `from <module> import
  <NAME>` against the exporting module's own bindings, for a module in the same test directory
  or at the `ansible/tests` root that `pythonpath` supplies. Resolution is one hop deep: a
  path two imports away is not followed, and a module whose own bindings need a third module's
  names does not resolve.

A glob over templates is NOT itself a read. A module that enumerates `templates/*.j2` to assert
on FILENAMES reads no bytes, and a render cannot answer a filename question, so the gate's
subject — an assertion matching template text — is not what such a census does.
"""

import ast
from pathlib import Path

_READ_METHODS = frozenset({"read_text", "read_bytes", "open"})
_GLOB_METHODS = frozenset({"glob", "rglob"})
# The calls that wrap an iterable without changing what it yields. A template glob inside one
# of these binds its loop target exactly as the bare glob does.
_WRAPPERS = frozenset(
    {"sorted", "list", "tuple", "set", "frozenset", "reversed", "iter"}
)
# Jinja's two entry points for a template string. A read whose result goes straight into one of
# these renders the template rather than asserting on its text, so it is not a source read:
# `setup/test_kuma_check_timer.py` renders its two units that way, with per-test overrides.
_RENDER_CALLS = frozenset({"from_string", "Template"})


def _unwrap(node: ast.expr) -> ast.expr:
    """`node` with any wrapping `sorted`/`list`/... calls stripped off its first argument."""
    while (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in _WRAPPERS
        and node.args
    ):
        node = node.args[0]
    return node


def _rendered_reads(tree: ast.Module) -> frozenset[int]:
    """The `id()` of every call nested in a Jinja render call's arguments.

    `env.from_string(path.read_text())` reads the template to RENDER it, and the assertions that
    follow are on the render. A read inside one of those arguments is therefore not a source
    read, wherever it is spelled.
    """
    found: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        called = (
            node.func.attr
            if isinstance(node.func, ast.Attribute)
            else getattr(node.func, "id", "")
        )
        if called not in _RENDER_CALLS:
            continue
        for arg in node.args:
            found.update(
                id(inner) for inner in ast.walk(arg) if isinstance(inner, ast.Call)
            )
    return frozenset(found)


def _names_a_template_path(node: ast.expr) -> bool:
    """Whether `node` builds a path that goes through a `templates/` directory or ends in `.j2`.

    A segment literal (`"templates"`), a slashed fragment (`"roles/k8s/x/templates/y.j2"`) and
    a bare `.j2` filename at the end of a path expression all count. A glob PATTERN is handled
    by `_glob_names_templates` instead: `"*.j2"` is a pattern, not a name.
    """
    for inner in ast.walk(node):
        if not (isinstance(inner, ast.Constant) and isinstance(inner.value, str)):
            continue
        value = inner.value
        if "*" in value:
            continue
        if (
            value == "templates"
            or "templates/" in value
            or value.endswith("/templates")
        ):
            return True
        if value.endswith(".j2"):
            return True
    return False


def _glob_names_templates(node: ast.Call) -> bool:
    """Whether a `glob`/`rglob` call enumerates templates, by its pattern."""
    return any(
        isinstance(a, ast.Constant)
        and isinstance(a.value, str)
        and (a.value.endswith(".j2") or "templates" in a.value)
        for a in node.args
    )


def _enumerates_templates(node: ast.expr, bound: dict[str, str]) -> bool:
    """Whether `node` yields template paths, by its glob pattern or by the directory it globs.

    The pattern alone misses `tdir.glob("*")` over a name bound to a `templates/` directory, so
    the receiver counts too — either because it names a template path or because it is already
    bound as one (#3211).
    """
    call = _unwrap(node)
    if not (
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr in _GLOB_METHODS
    ):
        return False
    receiver = call.func.value
    return (
        _glob_names_templates(call)
        or ast.unparse(receiver) in bound
        or _names_a_template_path(receiver)
    )


def _bindings(
    tree: ast.Module, imported: frozenset[str] = frozenset()
) -> dict[str, str]:
    """The names `tree` binds to a template path, by the expression each is bound to.

    Covers the spellings a reader takes: an assignment to a path expression, a `for` over a
    template glob (wrapped in `sorted()` or not), the same `for` inside a comprehension, a
    `for` over a TUPLE of names already bound this way — `_kuma_entities.py` reads its two
    templates that way, which is why the resolution runs to a fixed point rather than in one
    pass — and a name `imported` carries in from a sibling module.

    A path built under `tmp_path` is the module's own fixture rather than a deployed template,
    so it never binds: `test_nut_fsd_login_confined.py` lays out synthetic role trees.
    """
    pairs: list[tuple[list[ast.expr], ast.expr]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            pairs.append((node.targets, node.value))
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            pairs.append(([node.target], node.value))
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            pairs.append(([node.target], node.iter))

    bound: dict[str, str] = {name: name for name in imported}
    while True:
        grew = False
        for targets, value in pairs:
            text = ast.unparse(value)
            if "tmp_path" in text:
                continue
            hit = _enumerates_templates(value, bound)
            if not hit and isinstance(
                unwrapped := _unwrap(value), (ast.Tuple, ast.List, ast.Set)
            ):
                names = [e.id for e in unwrapped.elts if isinstance(e, ast.Name)]
                hit = len(names) == len(unwrapped.elts) > 0 and all(
                    name in bound for name in names
                )
            if not hit and isinstance(value, ast.Name):
                hit = value.id in bound
            if not hit:
                hit = _names_a_template_path(value)
            if not hit:
                continue
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in bound:
                    bound[target.id] = text
                    grew = True
        if not grew:
            return bound


def _params(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """The positional parameter names of `node`, in call order."""
    return [a.arg for a in (*node.args.posonlyargs, *node.args.args)]


def _reading_helpers(
    tree: ast.Module, rendered: frozenset[int]
) -> dict[str, frozenset[int]]:
    """The functions `tree` DEFINES that read a positional parameter as a file.

    Keyed by function name, valued by the argument positions that get read. A call to one with
    a template path at a reading position is a template source read, whatever the call site
    spells (#3209).
    """
    helpers: dict[str, frozenset[int]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        names = _params(node)
        reads = {
            index
            for index, name in enumerate(names)
            if any(
                id(child) not in rendered and _reads_the_name(child, name)
                for child in ast.walk(node)
            )
        }
        if reads:
            helpers[node.name] = frozenset(reads)
    return helpers


def _reads_the_name(node: ast.AST, name: str) -> bool:
    """Whether `node` is a read call whose subject is exactly the bare name `name`."""
    if not isinstance(node, ast.Call):
        return False
    if isinstance(node.func, ast.Name) and node.func.id == "open" and node.args:
        subject = node.args[0]
    elif isinstance(node.func, ast.Attribute) and node.func.attr in _READ_METHODS:
        subject = node.func.value
    else:
        return False
    return isinstance(subject, ast.Name) and subject.id == name


def _imported_names(tree: ast.Module) -> list[tuple[str, str, str]]:
    """Every `from <module> import <name> [as <alias>]` in `tree`, as (module, name, alias)."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                if alias.name != "*":
                    out.append((node.module, alias.name, alias.asname or alias.name))
    return out


def imported_template_names(source: str, siblings: dict[str, str]) -> frozenset[str]:
    """The names `source` imports from `siblings` that the exporting module binds to a template.

    `siblings` maps an importable module name to its text. A module not in it does not resolve,
    which is how a third-party or stdlib import stays out of the census.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return frozenset()
    carried = set()
    for module, name, alias in _imported_names(tree):
        text = siblings.get(module)
        if text is None:
            continue
        try:
            exported = _bindings(ast.parse(text))
        except SyntaxError:
            continue
        if name in exported:
            carried.add(alias)
    return frozenset(carried)


def sibling_sources(directory: Path, root: Path) -> dict[str, str]:
    """Every module importable by a test in `directory`, by module name.

    `directory`'s own modules, plus the shared ones at `root` — pytest prepends a test's own
    directory, and `pythonpath` supplies `ansible/tests`.
    """
    return {
        path.stem: path.read_text()
        for place in (root, directory)
        for path in sorted(place.glob("*.py"))
        if path.name != "__init__.py"
    }


def template_source_reads(
    source: str, siblings: dict[str, str] | None = None
) -> list[str]:
    """The expressions in `source` that read a template's SOURCE text, by how they spell it."""
    tree = ast.parse(source)
    bound = _bindings(tree, imported_template_names(source, siblings or {}))
    rendered = _rendered_reads(tree)
    helpers = _reading_helpers(tree, rendered)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or id(node) in rendered:
            continue
        if isinstance(node.func, ast.Name) and node.func.id in helpers:
            if any(
                "tmp_path" not in (text := ast.unparse(arg))
                and (text in bound or _names_a_template_path(arg))
                for index, arg in enumerate(node.args)
                if index in helpers[node.func.id]
            ):
                found.append(ast.unparse(node))
            continue
        if isinstance(node.func, ast.Name) and node.func.id == "open" and node.args:
            target = node.args[0]
        elif isinstance(node.func, ast.Attribute) and node.func.attr in _READ_METHODS:
            target = node.func.value
        else:
            continue
        text = ast.unparse(target)
        if "tmp_path" in text:
            continue
        if text in bound or _names_a_template_path(target):
            found.append(ast.unparse(node))
    return sorted(set(found))
