"""The modules a no-venv caller reaches, and the two checks that they import with the stdlib alone.

A hook `run-hook.sh` runs `--no-project`, a cc-wait source and fan-out's review and clean units
all run under `uv run --no-project`, which installs nothing. A third-party import anywhere they
reach dies at import, behind a green suite whose venv has the package. #4167 was one: fan-out
review died on `yaml`. Each break got its own one-path test after the fact (#4404); this module
derives the whole set instead.

- `stdlib_only_set()` maps each reached module to the directories its caller puts on
  `sys.path`: the entry's own directory (a by-path run) plus every `sys.path.insert` the
  reached modules make, evaluated statically. `scripts/lib/git.py` has no insert of its own
  and is reached through session-health's insert of `REPO/scripts`.
- `walk(path, dirs)` is the static layer: the imports of `path` that are none of the stdlib, a
  first-party module under `dirs`, a role module `repo_paths.ROLE_MODULES` lists for a
  directory in `dirs`, or `OUT_OF_TREE`.
- `runtime_import(path, dirs)` is the runtime layer: it imports `path` in a fresh interpreter
  with no site-packages and `PYTHONPATH` set to `dirs`.

The callers come from four places. `.claude/settings.json` names each `run-hook.sh` entry, and
one with no flag runs `--no-project`. Each `.claude/wait-sources/*` script names the file and
subcommand it runs. `FANOUT_UNITS` and `RUNBOOK_UNITS` list the legs whose command line is
built in Python or in an array of a shell script; `FANOUT_UNITS` lists the two fan-out legs built in
Python, which `scripts/lib/tests/test_stdlib_only.py` pins against the builders. The fourth is
the role scripts a host runs `--no-project` (#4455), which `lib.role_installs` reads from each
role's tasks and templates.

A role script runs from the path its role's `copy` task installs it to, so its `sys.path` is
the install directory, not its tree path. `lib.role_installs` copies every installed file to
that layout under a temporary root, and a role entry is reached from its staged copy, so an
import resolves only against what the role actually ships beside it. `stdlib_only_set()`
keys a staged member by its tree source; `members()` keeps each staged copy with its own
install's directories, because `host_lib.py` ships to a dozen of them and a union of their
directories would resolve an import through a sibling only another install carries.

The deployer's subprocess helpers (`narrow_setup.py`, `setup_routing.py`) are not callers: they run `uv run --frozen`, in
the venv, and `test_the_deployers_subprocess_helpers_run_in_the_venv` pins that.

Run: uv run pytest scripts/lib/tests/test_stdlib_only.py
"""

import ast
import functools
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import repo_paths, role_installs
from lib.repo_paths import REPO, ROLE_MODULES, ROLES

HOOKS = REPO / ".claude" / "hooks"
WAIT_SOURCES = REPO / ".claude" / "wait-sources"
# Modules a hook imports from outside the tree: `_claude_guard.py` inserts the dotfiles
# directory that holds them.
OUT_OF_TREE = frozenset({"claude_guard", "claude_worktree"})
# The fan-out legs `uv run --no-project` runs whose command line is built in Python, as
# (entry, argv): `launch.review_command` runs `fanout.py review`, and
# `clean.remote_clean_command` runs the `fanout_place.py` shim, which forwards to
# `fanout.py place`. The shim's forward is a call, not a dispatch `_dispatched` reads, so the
# place leg is listed as what it forwards to as well. `notify.stop_post` runs
# `fanout_lib/notify.py` directly, with no subcommand.
_DEV = REPO / "scripts" / "dev"
FANOUT_UNITS = (
    (_DEV / "fanout.py", ("review",)),
    (_DEV / "fanout_place.py", ("clean-one",)),
    (_DEV / "fanout.py", ("place", "clean-one")),
    (_DEV / "fanout_lib" / "notify.py", ()),
)
# The weekly etcd restore drill runs this gate under `uv run --no-project` when the checkout has
# no `.venv` (`scripts/backup/etcd_restore_drill.sh`'s `GATE_CMD`), as (entry, argv).
RUNBOOK_UNITS = (
    (REPO / "scripts" / "deploy_tools" / "runbook_gates.py", ("etcd-restore",)),
)
# A `run-hook.sh` entry with a flag runs `--project`, in the venv.
_HOOK_ENTRY = re.compile(r"/\.claude/hooks/run-hook\.sh ([\w.-]+)((?: --\S+)*)")
_WAIT_ENTRY = re.compile(r'--no-project\b.*\bpython "\$root/([^"]+)"((?: [\w-]+)*)')


def _hook_entries() -> list[tuple[Path, tuple[str, ...]]]:
    settings = json.loads(
        (REPO / ".claude" / "settings.json").read_text(encoding="utf-8")
    )
    found = []
    for groups in settings["hooks"].values():
        for group in groups:
            for hook in group["hooks"]:
                match = _HOOK_ENTRY.search(hook["command"])
                if match and not match.group(2).strip():
                    found.append((HOOKS / f"{match.group(1)}.py", ()))
    return found


def _wait_entries() -> list[tuple[Path, tuple[str, ...]]]:
    found = []
    for source in sorted(WAIT_SOURCES.iterdir()):
        for match in _WAIT_ENTRY.finditer(source.read_text(encoding="utf-8")):
            found.append((REPO / match.group(1), tuple(match.group(2).split())))
    return found


def entries() -> list[tuple[Path, tuple[str, ...]]]:
    """Every no-venv entry file, with the subcommand argv its caller passes."""
    return sorted(
        set(
            _hook_entries()
            + _wait_entries()
            + list(FANOUT_UNITS)
            + list(RUNBOOK_UNITS)
            + role_installs.role_entries()
        )
    )


@functools.cache
def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


# DECIDED: a library module contributes only the imports and inserts that run when it is
# imported, never those inside a function body. A call-time import is how a module keeps a
# venv-only dependency off the no-venv path (`fanout_lib/brief.py:_precomputed`, #4167), and
# following it would put the venv's whole reach in the set. An entry run with no subcommand
# contributes everything, because its `main` runs on every call; one run with a subcommand
# contributes its import-time code plus what that subcommand dispatches to (`_dispatched`).
def _nodes(node: ast.AST, call_time: bool) -> Iterator[ast.AST]:
    for child in ast.iter_child_nodes(node):
        yield child
        if call_time or not isinstance(
            child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
        ):
            yield from _nodes(child, call_time)


def _function(tree: ast.Module, name: str) -> ast.AST | None:
    return next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == name
        ),
        None,
    )


def _dispatched(tree: ast.Module, command: str) -> tuple[list[str], list[ast.AST]]:
    """What `command` reaches in an entry: module names a dict maps it to, and code it runs.

    Four dispatch shapes occur: `COMMANDS = {"review": "fanout_lib.review_unit"}` with an
    `importlib.import_module`, `if command == "probe":` around a call-time import, an argparse
    `add_parser("clean-one")` whose `set_defaults(fn=...)` names the function, and a registry
    row `("etcd-restore", "etcd_restore", ...)` whose module sits under a dotted string constant
    (`runbook_gates.py`'s `LIB`). A candidate that names no module resolves to nothing.
    """
    modules, code = [], []
    parsers: dict[str, str] = {}
    prefixes = [
        node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
        and "." in node.value.value
    ]
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Tuple)
            and len(node.elts) >= 2
            and isinstance(node.elts[0], ast.Constant)
            and node.elts[0].value == command
            and isinstance(node.elts[1], ast.Constant)
            and isinstance(node.elts[1].value, str)
        ):
            stem = node.elts[1].value
            modules += [f"{prefix}.{stem}" for prefix in prefixes]
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == command
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                ):
                    modules.append(value.value)
        elif isinstance(node, ast.If) and isinstance(node.test, ast.Compare):
            compared = [node.test.left, *node.test.comparators]
            if any(
                isinstance(c, ast.Constant) and c.value == command for c in compared
            ):
                code.extend(node.body)
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            call = node.value
            if (
                isinstance(call.func, ast.Attribute)
                and call.func.attr == "add_parser"
                and call.args
                and isinstance(call.args[0], ast.Constant)
                and isinstance(call.args[0].value, str)
                and isinstance(node.targets[0], ast.Name)
            ):
                parsers[node.targets[0].id] = call.args[0].value
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "set_defaults"
            and isinstance(node.func.value, ast.Name)
            and parsers.get(node.func.value.id) == command
        ):
            for keyword in node.keywords:
                if isinstance(keyword.value, ast.Name):
                    function = _function(tree, keyword.value.id)
                    if function is not None:
                        code.append(function)
    return modules, code


def _eval(
    node: ast.AST, path: Path, env: dict[str, str | Path | None]
) -> str | Path | None:
    """A `sys.path` insert's argument, for the idioms the tree uses; None when unknown.

    `script_imports._path_expr` reads only `Path(__file__)`-rooted spellings and skips a named
    constant by design. Here a named constant is the point (`GITOPS_DEPLOY_FILES` is how a
    role directory reaches the path), and session-health spells its inserts with `os.path`.
    """
    match node:
        case ast.Constant(value=str() as value):
            return value
        case ast.Name(id="__file__"):
            return path
        case ast.Name(id=name):
            value = env[name] if name in env else getattr(repo_paths, name, None)
            return value if isinstance(value, str | Path) else None
        case ast.Call(func=ast.Name(id=func), args=[arg]) if func in {
            "str",
            "Path",
            "_Path",
        }:
            return _as_path(_eval(arg, path, env))
        case ast.Call(func=ast.Attribute(attr="join"), args=args):
            values = [_eval(a, path, env) for a in args]
            parts = [v for v in values if v is not None]
            return Path(*parts) if parts and len(parts) == len(values) else None
        case ast.Call(func=ast.Attribute(attr="dirname"), args=[arg]):
            value = _as_path(_eval(arg, path, env))
            return None if value is None else value.parent
        case ast.Call(
            func=ast.Attribute(value=ast.Attribute(attr="environ"), attr="get"),
            args=[_, default],
        ):
            # `os.environ.get("DEPLOYER_DIR", "/opt/gitops-deploy")`: the host runs the default.
            return _eval(default, path, env)
        case ast.Call(func=ast.Attribute(attr="abspath" | "realpath"), args=[arg]):
            return _as_path(_eval(arg, path, env))
        case ast.Call(func=ast.Attribute(value=inner, attr="resolve")):
            return _as_path(_eval(inner, path, env))
        case ast.Attribute(value=inner, attr="parent"):
            value = _as_path(_eval(inner, path, env))
            return None if value is None else value.parent
        case ast.Subscript(
            value=ast.Attribute(value=inner, attr="parents"),
            slice=ast.Constant(value=int() as index),
        ):
            value = _as_path(_eval(inner, path, env))
            return None if value is None else value.parents[index]
        case ast.BinOp(left=left, op=ast.Div(), right=right):
            lhs, rhs = _as_path(_eval(left, path, env)), _eval(right, path, env)
            return None if lhs is None or rhs is None else lhs / rhs
    return None


def _as_path(value: str | Path | None) -> Path | None:
    return None if value is None else Path(value)


def _inserts(path: Path, nodes: list[ast.AST]) -> list[Path]:
    """The directories `sys.path.insert`/`append` calls among `nodes` add, where knowable.

    An insert this cannot evaluate adds nothing, which fails closed: an import that needed it
    no longer resolves, and `walk` reports it.
    """
    env: dict[str, str | Path | None] = {}
    for node in _tree(path).body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            env[node.targets[0].id] = _eval(node.value, path, env)
    found = []
    for node in nodes:
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"insert", "append"}
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "path"
            and node.args
        ):
            value = _as_path(_eval(node.args[-1], path, env))
            if value is not None:
                found.append(role_installs.installed(value).resolve())
    return found


def _imports(path: Path, nodes: list[ast.AST]) -> Iterator[tuple[str, list[str]]]:
    """Each import among `nodes` as (absolute dotted module, names imported from it)."""
    for node in nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, []
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                base = path.parent
                for _ in range(node.level - 1):
                    base = base.parent
                yield f"{_relative(base)}{module}", [a.name for a in node.names]
            else:
                yield module, [a.name for a in node.names]


def _relative(base: Path) -> str:
    """A relative import's anchor, as a `\0<dir>\0` marker `_files` resolves from `base`."""
    return f"\0{base}\0"


def _files(
    module: str, names: list[str], dirs: list[Path]
) -> tuple[Path, list[Path]] | None:
    """The directory `module` resolves under and every file importing it runs, or None."""
    if module.startswith("\0"):
        _, base, rest = module.split("\0")
        roots, parts = [Path(base)], [p for p in rest.split(".") if p]
    else:
        roots, parts = dirs, module.split(".")
    for root in roots:
        files, here, resolved = [], root, True
        for part in parts:
            here = here / part
            if here.with_suffix(".py").is_file():
                files.append(here.with_suffix(".py"))
                break
            if not here.is_dir():
                resolved = False
                break
            if (here / "__init__.py").is_file():
                files.append(here / "__init__.py")
        else:
            # Every part is a package, so each imported name may be a submodule of it.
            for name in names:
                if (here / f"{name}.py").is_file():
                    files.append(here / f"{name}.py")
                elif (here / name / "__init__.py").is_file():
                    files.append(here / name / "__init__.py")
        if resolved:
            return root, files
    return None


def _scope(path: Path, argv: tuple[str, ...] | None) -> tuple[list[ast.AST], list[str]]:
    """The nodes `path` runs for a caller passing `argv`, and the modules it dispatches to.

    `argv` None means a library import; `()` means an entry run with no subcommand.
    """
    tree = _tree(path)
    if argv == ():
        return list(_nodes(tree, call_time=True)), []
    nodes = list(_nodes(tree, call_time=False))
    if not argv:
        return nodes, []
    modules, code = _dispatched(tree, argv[0])
    for block in code:
        nodes += [block, *_nodes(block, call_time=True)]
    return nodes, modules


def _reach(entry: Path, argv: tuple[str, ...]) -> tuple[set[Path], list[Path]]:
    """Every file `entry` reaches when run with `argv`, and the `sys.path` its run builds."""
    dirs = [entry.parent.resolve()]
    while True:
        seen: set[Path] = set()
        todo: list[tuple[Path, tuple[str, ...] | None]] = [(entry.resolve(), argv)]
        before = list(dirs)
        while todo:
            path, args = todo.pop()
            if path in seen and args is None:
                continue
            seen.add(path)
            nodes, dispatched = _scope(path, args)
            for directory in _inserts(path, nodes):
                if directory not in dirs:
                    dirs.append(directory)
            for module, names in _imports(path, nodes):
                resolved = _files(module, names, dirs)
                if resolved:
                    todo += [(f.resolve(), None) for f in resolved[1]]
            for module in dispatched:
                resolved = _files(module, [], dirs)
                if resolved and resolved[1]:
                    *packages, target = resolved[1]
                    todo += [(f.resolve(), None) for f in packages]
                    todo.append((target.resolve(), tuple(args[1:]) if args else None))
        if dirs == before:
            return seen, dirs


@functools.cache
def members() -> list[tuple[Path, list[Path]]]:
    """Each file a no-venv caller reaches, as that caller runs it, with that caller's `sys.path`.

    A role script appears as its staged copy, once per install directory it ships to.
    """
    found: dict[Path, list[Path]] = {}
    for entry, argv in entries():
        reached, dirs = _reach(entry, argv)
        for member in reached:
            have = found.setdefault(member, [])
            have += [d for d in dirs if d not in have]
    return sorted(found.items())


@functools.cache
def stdlib_only_set() -> dict[Path, list[Path]]:
    """Each module a no-venv caller reaches, mapped to the directories that caller puts on `sys.path`.

    Keyed by the tree file: a staged copy is keyed by its source, with every install's
    directories. `members()` keeps the installs apart, for the checks.
    """
    found: dict[Path, list[Path]] = {}
    for member, dirs in members():
        have = found.setdefault(role_installs.tree_source(member), [])
        have += [d for d in dirs if d not in have]
    return found


def colliding_basenames() -> frozenset[str]:
    """Module basenames two or more of `repo_paths`' role directories ship."""
    seen: dict[str, int] = {}
    role_dirs = {
        value.resolve()
        for name, value in vars(repo_paths).items()
        if name.isupper()
        and isinstance(value, Path)
        and value.is_dir()
        and value.resolve().is_relative_to(ROLES)
    }
    for directory in role_dirs:
        for child in directory.glob("*.py"):
            seen[child.stem] = seen.get(child.stem, 0) + 1
    return frozenset(name for name, count in seen.items() if count > 1)


def _role_module(top: str, dirs: list[Path]) -> bool:
    """Whether `top` resolves to exactly one of `dirs`, and the map lists it for that one.

    Exactly one, because the static walk does not model `sys.path` order: with both
    MONITOR_BRIDGE_FILES and GITOPS_DEPLOY_FILES on the path, which `gitops_hold` loads is
    the question the map exists to answer, so two holders are refused rather than guessed.
    """
    holders = [d for d in dirs if (d / f"{top}.py").is_file() or (d / top).is_dir()]
    return len(holders) == 1 and any(
        directory.resolve() == holders[0] and top in names
        for directory, names in ROLE_MODULES.items()
    )


_IMPORT_ERRORS = {"ImportError", "ModuleNotFoundError"}


def _ends_the_run(node: ast.AST) -> bool:
    """Whether `node` is a `raise` or a call to `sys.exit`, `exit` or `SystemExit`."""
    if isinstance(node, ast.Raise):
        return True
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    return name in {"exit", "SystemExit", "_exit"}


# DECIDED: an import inside a `try` whose handler names ImportError or ModuleNotFoundError
# cannot kill the caller at import, which is the failure this module exists to catch. The
# deployer's `deploy_setup_roles.routing` imports `setup_routing` that way, and its install
# directory deliberately ships none. A handler for any other exception guards nothing, and
# neither does one that raises or exits: `except ImportError: raise SystemExit(...)` still dies.
def _guarded(tree: ast.Module) -> set[int]:
    """The ids of the import nodes a `try ... except ImportError` guards."""
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        guards = []
        for handler in node.handlers:
            kinds = (
                handler.type.elts
                if isinstance(handler.type, ast.Tuple)
                else [handler.type]
            )
            if any(isinstance(k, ast.Name) and k.id in _IMPORT_ERRORS for k in kinds):
                guards.append(not any(map(_ends_the_run, ast.walk(handler))))
        if guards and all(guards):
            for statement in node.body:
                found |= {
                    id(sub)
                    for sub in ast.walk(statement)
                    if isinstance(sub, (ast.Import, ast.ImportFrom))
                }
    return found


def walk(path: Path, dirs: list[Path]) -> list[str]:
    """The imports in `path` that a no-venv run cannot be shown to satisfy.

    The walk reads every import, call-time ones included: a function-local import of a
    first-party module resolves and passes, but a function-local `import yaml` does not.
    An import guarded by `except ImportError` is skipped (`_guarded`).
    """
    dirs = [Path(d).resolve() for d in dirs]
    colliding = colliding_basenames()
    guarded = _guarded(_tree(path))
    nodes = [n for n in ast.walk(_tree(path)) if id(n) not in guarded]
    bad = []
    for module, names in _imports(path, nodes):
        if module.startswith("\0"):
            if _files(module, names, dirs) is None:
                bad.append(module.split("\0")[2] or ".")
            continue
        top = module.partition(".")[0]
        if top in sys.stdlib_module_names or top in OUT_OF_TREE or top == "__future__":
            continue
        if _role_module(top, dirs):
            continue
        resolved = _files(module, names, dirs)
        # DECIDED: an import resolving inside a staged install directory passes, a basename
        # two roles share included. The role's copy task chose which file sits there, so the
        # host has exactly one candidate and `sys.path` order is not a question.
        if resolved is not None and resolved[0].is_relative_to(role_installs.stage()):
            continue
        if resolved is None or top in colliding or resolved[0].is_relative_to(ROLES):
            bad.append(module)
    return sorted(set(bad))


def _module_name(path: Path, dirs: list[Path]) -> str | None:
    """The dotted name `path` imports as under the deepest of `dirs` holding it."""
    for directory in sorted(dirs, key=lambda d: len(d.parts), reverse=True):
        if path.is_relative_to(directory):
            parts = path.relative_to(directory).with_suffix("").parts
            if parts[-1] == "__init__":
                parts = parts[:-1]
            if parts and all(part.isidentifier() for part in parts):
                return ".".join(parts)
    return None


_BY_PATH = (
    "import importlib.util, sys; p = sys.argv[1]; "
    "s = importlib.util.spec_from_file_location('stdlib_only_probe', p); "
    "m = importlib.util.module_from_spec(s); s.loader.exec_module(m)"
)


def runtime_import(path: Path, dirs: list[Path]) -> tuple[bool, str]:
    """Import `path` with no site-packages and `PYTHONPATH` set to `dirs`: (imported, stderr).

    `-S` leaves out site-packages, the same interpreter `uv run --no-project` gives with no
    third-party package on the path; `-P` keeps the cwd off `sys.path`. A module whose name is
    not importable (`session-health.py`) is loaded by path, without running its `__main__`.
    """
    path = path.resolve()
    dirs = [Path(d).resolve() for d in dirs]
    name = _module_name(path, dirs)
    # DECIDED: a module that raises SystemExit at import counts as imported, unless the exit
    # came from a failed import. Every import it ran before the exit resolved, and `walk` reads
    # the rest. `longhorn_backup_health.py` exits on an env var its cron wrapper exports, after
    # all of its imports.
    code = (
        f"try:\n    {f'import {name}' if name else _BY_PATH}\n"
        "except SystemExit as e:\n"
        "    if isinstance(e.__context__, ImportError):\n"
        "        raise"
    )
    env = {
        k: v for k, v in os.environ.items() if k not in {"VIRTUAL_ENV", "PYTHONPATH"}
    }
    env |= role_installs.staged_overrides()
    env["PYTHONPATH"] = os.pathsep.join(str(d) for d in dirs)
    done = subprocess.run(
        [sys.executable, "-S", "-P", "-c", code, str(path)],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        cwd="/",
        check=False,
    )
    return done.returncode == 0, done.stderr
