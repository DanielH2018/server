"""The AST detectors behind the three render-and-path rules, apart from the census.

`test_census_rows_test_renders.py` states the rules, holds the exemptions and runs each as a
`Census` row over the tree; this module is the pure half each rule needs — a function from source
text to the offending spellings it found. Split out when the detector for a shell template's
source grew the resolver-fed shape (#3200) and pushed the one file past its 500-line cap.

Every detector takes TEXT rather than a path, so a test can hand it a two-line snippet. That is
what the per-spelling tests next to each rule do, and it is why a rule's rejecting half costs
nothing to write.
"""

import ast
from pathlib import Path
from typing import NamedTuple

from lib.repo_paths import REPO

_ENV_CLASSES = frozenset({"Environment", "NativeEnvironment"})
_JINJA_MODULES = frozenset({"jinja2", "jinja2.nativetypes"})


def bare_jinja_envs(source: str) -> list[str]:
    """The Jinja environment classes `source` constructs directly, by the name it calls.

    A name counts only when it was imported from `jinja2` or `jinja2.nativetypes`, so
    `liquid.Environment()` — a different template language — is not a hit.
    """
    tree = ast.parse(source)
    bound: set[str] = set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in _JINJA_MODULES:
            bound.update(
                a.asname or a.name for a in node.names if a.name in _ENV_CLASSES
            )
        elif isinstance(node, ast.Import):
            modules.update(
                a.asname or a.name for a in node.names if a.name in _JINJA_MODULES
            )
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in bound:
            found.append(func.id)
        elif (
            isinstance(func, ast.Attribute)
            and func.attr in _ENV_CLASSES
            and ast.unparse(func.value) in modules
        ):
            found.append(ast.unparse(func))
    return found


def _eval_file_path(node: ast.expr, module: Path) -> Path | None:
    """The path `node` computes from `__file__` in `module`, or None if it is not that shape.

    Understands `Path(__file__)`, `.resolve()`, `.absolute()`, `.parent`, `.parents[n]`, and
    the `os.path.dirname`/`abspath`/`realpath` spelling of the same walk.
    """
    if isinstance(node, ast.Name) and node.id == "__file__":
        return module
    if isinstance(node, ast.Call):
        name = ast.unparse(node.func)
        if name in {"Path", "pathlib.Path"} and len(node.args) == 1:
            return _eval_file_path(node.args[0], module)
        if name in {"os.path.abspath", "os.path.realpath"} and len(node.args) == 1:
            return _eval_file_path(node.args[0], module)
        if name == "os.path.dirname" and len(node.args) == 1:
            inner = _eval_file_path(node.args[0], module)
            return None if inner is None else inner.parent
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in {"resolve", "absolute"}
            and not node.args
        ):
            return _eval_file_path(node.func.value, module)
        return None
    if isinstance(node, ast.Attribute) and node.attr == "parent":
        inner = _eval_file_path(node.value, module)
        return None if inner is None else inner.parent
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "parents"
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, int)
    ):
        inner = _eval_file_path(node.value.value, module)
        if inner is None or node.slice.value >= len(inner.parents):
            return None
        return inner.parents[node.slice.value]
    return None


def local_repo_roots(source: str, module: Path, repo: Path = REPO) -> list[str]:
    """The names `source` binds to `repo` by walking up from `__file__` at `module`."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if _eval_file_path(value, module) == repo:
            found.extend(ast.unparse(t) for t in targets)
    return found


# How a test gets at a shell template's text. `read_bytes` and `open` count: the rule is about
# reading the file, not about the decoding. `glob("*.sh.j2")` is the roster spelling of the same
# thing — a module that enumerates the templates itself is one that will read them next.
_READ_METHODS = frozenset({"read_text", "read_bytes", "open"})
_GLOB_METHODS = frozenset({"glob", "rglob"})

# Functions that hand back template PATHS off the real tree, by their terminal name. A read
# through one is a source read even though no `*.sh.j2` literal appears anywhere in the module
# (#3200). The terminal name is what matches, so `ct.iter_cron_targets`, a bare
# `iter_cron_targets` and any alias of either are the same hit.
#
# `discover_templates` is three different functions — `validate.shell_templates`,
# `validate.setup_templates` and `validate.unit_templates` each define one over a different
# file set. Matching the bare name over-matches the setup and unit planes deliberately: no test
# reads through those today, and one that starts gets an exemption with its reason, which is
# cheaper than teaching the matcher which module an alias came from.
#
# `validate.jinja_bash_collisions.templates` is deliberately NOT here. It resolves every `*.j2`
# in the tree, and the collision it looks for (`${#x}` meeting Jinja) is a property of the
# source that a render cannot answer, so declaring that read beats flagging it.
_TEMPLATE_RESOLVERS = frozenset(
    {"iter_cron_targets", "cron_job_scripts", "discover_templates"}
)
_KEY_METHODS = frozenset({"items", "keys"})


def _resolver_call(node: ast.expr) -> str | None:
    """The unparsed text of `node` when it calls a template resolver over the REAL tree.

    A call passed a `tmp_path` root resolves a synthetic role tree the module built itself, so
    reading through it is the same fixture read `_shell_template_paths` already lets past.
    """
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    if name not in _TEMPLATE_RESOLVERS:
        return None
    text = ast.unparse(node)
    return None if "tmp_path" in text else text


def _resolver_returning_fixtures(tree: ast.Module) -> dict[str, str]:
    """The pytest fixtures in `tree` that hand back a template resolver's result, by name.

    A fixture's name is the name its consumers take the value under, so binding the name here
    puts a consumer's `for tpl in cron_map:` within reach of the loop clause below (#3206).
    Both fixture shapes count: a `return` and the `yield` a fixture with teardown uses.

    A decorator counts when its text mentions `fixture`, which covers `@pytest.fixture`,
    `@fixture` and the parametrised `@pytest.fixture(scope="module")` alike.
    """
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not any("fixture" in ast.unparse(d) for d in node.decorator_list):
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, (ast.Return, ast.Yield)) or inner.value is None:
                continue
            if (call := _resolver_call(inner.value)) is not None:
                found[node.name] = call
    return found


def _resolver_fed_names(tree: ast.Module) -> dict[str, str]:
    """The names `tree` binds to a template path by looping over a resolver's result.

    Two shapes, both from #3200: a loop straight over the resolver call, and a loop over a name
    an assignment bound to one. A tuple-unpack binds only its FIRST element — every resolver
    here yields the template first (`iter_cron_targets` yields
    `(template, task_file, cron, env)`, `cron_job_scripts` is keyed by template), and binding
    `task_file` too would read a task-file read as a shell-template read.

    A resolver result that arrives as a FUNCTION PARAMETER is reachable too, through the
    fixture that produced it (#3206). `_resolver_returning_fixtures` resolves each fixture's
    return expression once per module and binds the fixture's NAME, which is the name every
    consumer takes the value under, so a loop in a consumer's body reads as a loop over the
    resolver. The `cron_map` fixture in
    `scripts/validate/tests/test_shell_template_cron_rules.py` is the worked example.
    """
    held: dict[str, str] = _resolver_returning_fixtures(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if (call := _resolver_call(value)) is not None:
            for target in targets:
                if isinstance(target, ast.Name):
                    held[target.id] = call
    bound: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            continue
        iterable = node.iter
        source = _resolver_call(iterable)
        if source is None and isinstance(iterable, ast.Name):
            source = held.get(iterable.id)
        if (
            source is None
            and isinstance(iterable, ast.Call)
            and isinstance(iterable.func, ast.Attribute)
            and iterable.func.attr in _KEY_METHODS
            and isinstance(iterable.func.value, ast.Name)
        ):
            source = held.get(iterable.func.value.id)
        if source is None:
            continue
        target = node.target
        if isinstance(target, ast.Tuple) and target.elts:
            target = target.elts[0]
        if isinstance(target, ast.Name):
            bound[target.id] = source
    return bound


def _names_a_shell_template(node: ast.expr) -> bool:
    """Whether `node` holds a `*.sh.j2` FILENAME literal, as opposed to a glob pattern.

    `"*.sh.j2"` is a pattern rather than a name, and is handled by the glob clause below.
    """
    return any(
        isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and n.value.endswith(".sh.j2")
        and "*" not in n.value
        for n in ast.walk(node)
    )


def _is_shell_template_path(value: ast.expr) -> bool:
    """Whether `value` is a path EXPRESSION that ends in a deployed `*.sh.j2` file.

    `ROLES / "setup" / "k3s" / "templates" / "x.sh.j2"`, or the same walk written as one
    `Path("...")` argument. A bare filename string is a name, not a path: `_shell_render`'s own
    (plane, role, name) triples are spelled that way, as is every roster that names the
    templates it expects to find.

    A template the module wrote under `tmp_path` is its own fixture rather than a deployed
    script: the validator's tests build synthetic role trees, and reading one back is how they
    check the renderer.
    """
    is_path = isinstance(value, ast.BinOp) and isinstance(value.op, ast.Div)
    is_path = is_path or (
        isinstance(value, ast.Call) and ast.unparse(value.func).endswith("Path")
    )
    return (
        is_path
        and _names_a_shell_template(value)
        and "tmp_path" not in ast.unparse(value)
    )


def _shell_template_paths(tree: ast.Module) -> dict[str, str]:
    """The names `tree` binds to a path that ends in a `*.sh.j2` file."""
    bound: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if _is_shell_template_path(value):
            for target in targets:
                if isinstance(target, ast.Name):
                    bound[target.id] = ast.unparse(value)
    return bound


def _shadowed_params(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda,
) -> set[str]:
    """The parameter names `node` binds, which shadow any module-level name of the same name."""
    args = node.args
    named = {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
    return named | {a.arg for a in (args.vararg, args.kwarg) if a is not None}


class _ReadingHelper(NamedTuple):
    """One locally defined function that reads a parameter as a file.

    Attributes:
        positions: Every parameter name in declaration order, which resolves a positional
          argument at a call site to the parameter it lands on.
        read: The parameter names the body reads as a file.
    """

    positions: tuple[str, ...]
    read: frozenset[str]


def _reading_helpers(tree: ast.Module) -> dict[str, _ReadingHelper]:
    """The functions `tree` itself defines that read a parameter as a file, by function name.

    A read inside such a helper is invisible to the clauses above — the parameter shadows the
    name the caller passed, so `_source_read_calls` drops the binding — and the call site names
    no `*.sh.j2` of its own. `ansible/tests/deploy/test_setup_drift_check.py` was the live
    instance: a module-level `_CHECK` path, a local `_source(path)` that stripped comments out
    of `path.read_text()`, and four tests asserting on the result. It read a source through two
    directory-wide conversions and the rule never saw it (#3220). So the helper's own read is
    recorded here, and the CALL to it counts as the read instead.

    Only a function THIS module defines qualifies. An imported one has no body to read, and
    resolving it would flag every `render_shell_script("setup", "k3s", "drill.sh.j2")` call,
    whose `name` argument ends in `.sh.j2`.

    Both parameter kinds a caller can reach count — positional and keyword-only — because
    `_source(path=_CHECK)` is otherwise a one-word way out of the clause.
    """
    found: dict[str, _ReadingHelper] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        args = node.args
        named = tuple(a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs))
        read = frozenset(
            name
            for name in named
            for inner in ast.walk(node)
            if isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Attribute)
            and inner.func.attr in _READ_METHODS
            and ast.unparse(inner.func.value) == name
        )
        if read:
            found[node.name] = _ReadingHelper(named, read)
    return found


def _helper_argument(call: ast.Call, helper: _ReadingHelper) -> ast.expr | None:
    """The argument `call` passes for a parameter `helper` reads, or None if it passes none."""
    for keyword in call.keywords:
        if keyword.arg in helper.read:
            return keyword.value
    for index, arg in enumerate(call.args):
        if index < len(helper.positions) and helper.positions[index] in helper.read:
            return arg
    return None


def _source_read_calls(
    node: ast.AST,
    bound: dict[str, str],
    helpers: dict[str, _ReadingHelper] | None = None,
) -> list[str]:
    """Every read of a bound template name under `node`, descending scope by scope.

    A function PARAMETER of the same name is a different object, so it drops the binding for
    that function's body. `_shell_render.rendered_or_source_text(path)` is why: the module
    binds `path` in two `for path in discover_templates()` loops, and that function's own
    `path.read_text()` is the fallback for an extension this module does not render.

    `helpers` maps a locally defined reading helper to the parameters it reads as a file, and a
    call that hands one of those a bound template name is a read at the call site. A parameter
    shadows a helper name the same way it shadows a bound name, so both maps narrow together.
    """
    helpers = {} if helpers is None else helpers
    found = []
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            shadowed = _shadowed_params(child)
            inner = {name: how for name, how in bound.items() if name not in shadowed}
            inner_helpers = {
                name: helper for name, helper in helpers.items() if name not in shadowed
            }
            found.extend(_source_read_calls(child, inner, inner_helpers))
            continue
        if (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Name)
            and child.func.id in helpers
        ):
            arg = _helper_argument(child, helpers[child.func.id])
            if arg is not None and (
                ast.unparse(arg) in bound or _is_shell_template_path(arg)
            ):
                found.append(ast.unparse(child))
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
            receiver = ast.unparse(child.func.value)
            reads_a_bound_name = child.func.attr in _READ_METHODS and (
                receiver in bound or _names_a_shell_template(child.func.value)
            )
            globs_the_roster = child.func.attr in _GLOB_METHODS and any(
                isinstance(a, ast.Constant)
                and isinstance(a.value, str)
                and a.value.endswith(".sh.j2")
                for a in child.args
            )
            if "tmp_path" not in receiver and (reads_a_bound_name or globs_the_roster):
                found.append(ast.unparse(child))
        found.extend(_source_read_calls(child, bound, helpers))
    return found


def shell_template_source_reads(source: str) -> list[str]:
    """The expressions in `source` that read a `*.sh.j2`'s SOURCE text, by how they spell it.

    A template written to `tmp_path` is the module's own fixture rather than a deployed script,
    so a receiver naming it is not a hit: the validator's tests build synthetic trees.
    """
    tree = ast.parse(source)
    bound = {**_shell_template_paths(tree), **_resolver_fed_names(tree)}
    return sorted(set(_source_read_calls(tree, bound, _reading_helpers(tree))))


# The names a test module binds the repo root or an anchor under it to, by the spelling
# `local_repo_roots` and the `repo_paths` imports leave in the tree.
_PATH_ANCHORS = frozenset({"REPO", "_REPO", "REPO_ROOT", "ANSIBLE", "ROLES"})
# The paths `lib.repo_paths` owns, by the segments that spell each, and the constant to import.
_OWNED_PATHS = (
    (("inventory", "hosts.ini"), "HOSTS_INI"),
    (("group_vars", "all.yml"), "ALL_VARS"),
    (("host_vars",), "HOST_VARS"),
    (("setup", "k3s", "defaults", "main.yml"), "K3S_DEFAULTS"),
)


def _anchored_segments(node: ast.BinOp) -> list[str | None] | None:
    """The path segments of a `/` chain rooted at a repo anchor, or None for any other chain.

    The root is a bare anchor name or `Path(<anchor>)`. A part that is not a string literal is
    a `None` segment, so it separates two spelled runs rather than joining them.
    """
    parts: list[ast.expr] = []
    left: ast.expr = node
    while isinstance(left, ast.BinOp) and isinstance(left.op, ast.Div):
        parts.append(left.right)
        left = left.left
    if isinstance(left, ast.Call) and len(left.args) == 1 and not left.keywords:
        func = left.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        left = left.args[0] if name == "Path" else left
    if not (isinstance(left, ast.Name) and left.id in _PATH_ANCHORS):
        return None
    segments: list[str | None] = []
    for part in reversed(parts):
        if isinstance(part, ast.Constant) and isinstance(part.value, str):
            segments.extend(s for s in part.value.split("/") if s)
        else:
            segments.append(None)
    return segments


def _spells(segments: list[str | None], needle: tuple[str, ...]) -> bool:
    width = len(needle)
    return any(
        tuple(segments[i : i + width]) == needle
        for i in range(len(segments) - width + 1)
    )


def inline_inventory_paths(source: str) -> list[str]:
    """The `/` chains `source` builds to a path `lib.repo_paths` owns, each with its constant.

    A hit is a chain rooted at the repo or an anchor under it whose string parts spell the
    inventory, `group_vars/all.yml`, `host_vars` or the k3s role's defaults, whether the parts
    are one string or several. Only the outermost chain is read, so one expression is one hit.
    A repo-relative STRING (`"ansible/inventory/group_vars/all.yml"`) is not a chain and not a
    hit: the deploy classifiers' tests hand those to the code under test as inputs.
    """
    tree = ast.parse(source)
    inner = {
        id(node.left)
        for node in ast.walk(tree)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
    }
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.BinOp) or id(node) in inner:
            continue
        segments = _anchored_segments(node)
        if segments is None:
            continue
        found += [
            f"{ast.unparse(node)} (import {constant} from lib.repo_paths)"
            for needle, constant in _OWNED_PATHS
            if _spells(segments, needle)
        ]
    return found
