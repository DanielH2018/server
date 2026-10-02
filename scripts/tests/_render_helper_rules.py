"""The AST detectors behind the three render-and-path rules, apart from the census.

`test_tests_share_render_and_path_helpers.py` states the rules, holds the exemption maps and
runs them over the tree; this module is the pure half each rule needs — a function from source
text to the offending spellings it found. Split out when the detector for a shell template's
source grew the resolver-fed shape (#3200) and pushed the one file past its 500-line cap.

Every detector takes TEXT rather than a path, so a test can hand it a two-line snippet. That is
what the per-spelling tests next to each rule do, and it is why a rule's rejecting half costs
nothing to write.
"""

import ast
from pathlib import Path

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


def _resolver_fed_names(tree: ast.Module) -> dict[str, str]:
    """The names `tree` binds to a template path by looping over a resolver's result.

    Two shapes, both from #3200: a loop straight over the resolver call, and a loop over a name
    an assignment bound to one. A tuple-unpack binds only its FIRST element — every resolver
    here yields the template first (`iter_cron_targets` yields
    `(template, task_file, cron, env)`, `cron_job_scripts` is keyed by template), and binding
    `task_file` too would read a task-file read as a shell-template read.

    A resolver result that arrives as a FUNCTION PARAMETER is out of reach: the `cron_map`
    fixture in `scripts/validate/tests/test_shell_template_cron_rules.py` returns
    `cron_job_scripts()`, and its consumers take it as an argument. Nothing reads a template's
    text through that fixture today — its loops render instead — and tracking a fixture's
    return through every consumer's signature is out of proportion to the one shape it buys.
    """
    held: dict[str, str] = {}
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


def _shell_template_paths(tree: ast.Module) -> dict[str, str]:
    """The names `tree` binds to a path that ends in a `*.sh.j2` file.

    Only a path EXPRESSION counts — `ROLES / "setup" / "k3s" / "templates" / "x.sh.j2"`, or the
    same walk written as one `Path("...")` argument. A bare filename string is a name, not a
    path: `_shell_render`'s own (plane, role, name) triples are spelled that way, as is every
    roster that names the templates it expects to find.
    """
    bound: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        is_path = isinstance(value, ast.BinOp) and isinstance(value.op, ast.Div)
        is_path = is_path or (
            isinstance(value, ast.Call) and ast.unparse(value.func).endswith("Path")
        )
        # A template the module wrote under `tmp_path` is its own fixture rather than a deployed
        # script: the validator's tests build synthetic role trees, and reading one back is how
        # they check the renderer.
        if (
            is_path
            and _names_a_shell_template(value)
            and "tmp_path" not in ast.unparse(value)
        ):
            for target in targets:
                if isinstance(target, ast.Name):
                    bound[target.id] = ast.unparse(value)
    return bound


def _shadowed_params(node: ast.AST) -> set[str]:
    """The parameter names `node` binds, which shadow any module-level name of the same name."""
    args = node.args
    named = {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
    return named | {a.arg for a in (args.vararg, args.kwarg) if a is not None}


def _source_read_calls(node: ast.AST, bound: dict[str, str]) -> list[str]:
    """Every read of a bound template name under `node`, descending scope by scope.

    A function PARAMETER of the same name is a different object, so it drops the binding for
    that function's body. `_shell_render.rendered_or_source_text(path)` is why: the module
    binds `path` in two `for path in discover_templates()` loops, and that function's own
    `path.read_text()` is the fallback for an extension this module does not render.
    """
    found = []
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            inner = {
                name: how
                for name, how in bound.items()
                if name not in _shadowed_params(child)
            }
            found.extend(_source_read_calls(child, inner))
            continue
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
        found.extend(_source_read_calls(child, bound))
    return found


def shell_template_source_reads(source: str) -> list[str]:
    """The expressions in `source` that read a `*.sh.j2`'s SOURCE text, by how they spell it.

    A template written to `tmp_path` is the module's own fixture rather than a deployed script,
    so a receiver naming it is not a hit: the validator's tests build synthetic trees.
    """
    tree = ast.parse(source)
    bound = {**_shell_template_paths(tree), **_resolver_fed_names(tree)}
    return sorted(set(_source_read_calls(tree, bound)))
