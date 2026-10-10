"""The AST detector behind the census row that keeps inventory paths in `lib.repo_paths`.

`test_census_rows_test_renders.py` runs `inline_inventory_paths` as the
`tests-take-inventory-paths-from-repo-paths` row over `pytest_only_modules()` (#4002). It sits
apart from `_render_helper_rules.py`, whose detectors fill that module to its 500-line cap. Like
them, it takes TEXT, so a row's red and green subjects can be one-line snippets; the module's
location is an optional second argument, read only to resolve `Path(__file__)` arithmetic.
"""

import ast
from pathlib import Path

from lib.repo_paths import REPO

# The `lib.repo_paths` names a test module reads the tree through, each with the segments it
# stands for. A chain's segments start with those, so `INVENTORY / "hosts.ini"` spells the
# same path as `REPO / "ansible/inventory/hosts.ini"`. `_REPO` and `REPO_ROOT` are the
# spellings `local_repo_roots` leaves behind, and `SETUP_ROLES` is `ansible/tests/_helpers`'
# own. A module's aliases of these are resolved per module.
_PATH_ANCHORS = {
    "REPO": (),
    "_REPO": (),
    "REPO_ROOT": (),
    "ANSIBLE": ("ansible",),
    "ROLES": ("ansible", "roles"),
    "SETUP_ROLES": ("ansible", "roles", "setup"),
    "INVENTORY": ("ansible", "inventory"),
    "K3S_ROLE": ("ansible", "roles", "setup", "k3s"),
}
# The paths `lib.repo_paths` owns, by the segments that spell each, and the constant to import.
# The k3s entries run most specific first, and a chain reports only its first match, so
# `ROLES / "setup/k3s/defaults/main.yml"` is one hit naming `K3S_DEFAULTS`.
_OWNED_PATHS = (
    (("inventory", "hosts.ini"), "HOSTS_INI"),
    (("group_vars", "all.yml"), "ALL_VARS"),
    (("host_vars",), "HOST_VARS"),
    (("setup", "k3s", "defaults", "main.yml"), "K3S_DEFAULTS"),
    (("setup", "k3s", "files"), "K3S_FILES"),
    (("setup", "k3s"), "K3S_ROLE"),
)

Segments = tuple[str, ...]


def _root(
    node: ast.expr, names: dict[str, Segments], module: Path | None
) -> Segments | None:
    """The repo-relative segments the root of a `/` chain stands for, or None if unknown.

    A root is an anchor or a name the module bound to one, `v.ROLES` off an imported module,
    `__file__`, or any of those under `Path(...)`, `.resolve()`, `.parent` or `.parents[n]`.
    """
    if isinstance(node, ast.Name):
        if node.id == "__file__" and module is not None and module.is_relative_to(REPO):
            return module.relative_to(REPO).parts
        return names.get(node.id)
    if isinstance(node, ast.Attribute):
        if node.attr == "parent":
            inner = _root(node.value, names, module)
            return inner[:-1] if inner else None
        return _PATH_ANCHORS.get(node.attr)
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "parents"
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, int)
    ):
        inner = _root(node.value.value, names, module)
        drop = node.slice.value + 1
        return inner[:-drop] if inner is not None and len(inner) >= drop else None
    if isinstance(node, ast.Call) and not node.keywords:
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in {"resolve", "absolute"}:
            return None if node.args else _root(func.value, names, module)
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        # The terminal name, so `pathlib.Path` and an `import Path as _Path` alias count.
        if name.endswith("Path") and len(node.args) == 1:
            return _root(node.args[0], names, module)
    return None


def _chain(
    node: ast.expr, names: dict[str, Segments], module: Path | None
) -> tuple[int, list[str | None]] | None:
    """A `/` chain's root width and full segments, or None when its root is not in the repo.

    A part that is not a string literal is a `None` segment, so it separates two spelled runs
    rather than joining them. The root width says which segments the chain spelled itself.
    """
    parts: list[ast.expr] = []
    left = node
    while isinstance(left, ast.BinOp) and isinstance(left.op, ast.Div):
        parts.append(left.right)
        left = left.left
    root = _root(left, names, module)
    if root is None:
        return None
    segments: list[str | None] = list(root)
    for part in reversed(parts):
        if isinstance(part, ast.Constant) and isinstance(part.value, str):
            segments.extend(s for s in part.value.split("/") if s)
        else:
            segments.append(None)
    return len(root), segments


def _local_anchors(tree: ast.Module, module: Path | None) -> dict[str, Segments]:
    """The anchors plus every name `tree` binds to a path in the repo, at any scope.

    `from _helpers import ROLES as _ROLES` binds an alias, and `K3S = ROLES / "setup" /
    "k3s"` binds a name a later chain is rooted at (#4135). The bindings resolve to a fixed
    point, so a name bound through another local resolves too. A name bound to two different
    things anywhere in the module is dropped, because the walk does not know which scope a
    chain reads it from.
    """
    names = dict(_PATH_ANCHORS)
    bindings: list[tuple[str, ast.expr]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.update(
                (a.asname, _PATH_ANCHORS[a.name])
                for a in node.names
                if a.asname and a.name in _PATH_ANCHORS
            )
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            if isinstance(node.targets[0], ast.Name):
                bindings.append((node.targets[0].id, node.value))
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            if isinstance(node.target, ast.Name):
                bindings.append((node.target.id, node.value))
    # A binding that extends itself (`R = R / "x"`) never settles, so the rounds are bounded.
    # Bindings that only read each other settle within one round per binding.
    for _ in range(len(bindings) + 1):
        resolved: dict[str, set[Segments | None]] = {}
        for name, value in bindings:
            chain = _chain(value, names, module)
            spelled = [] if chain is None else [s for s in chain[1] if s is not None]
            path = tuple(spelled) if chain and len(spelled) == len(chain[1]) else None
            resolved.setdefault(name, set()).add(path)
        changed = False
        for name, paths in resolved.items():
            path = next(iter(paths)) if len(paths) == 1 else None
            if path is not None and names.get(name) != path:
                names[name], changed = path, True
        if not changed:
            break
    return names


def _spells(
    segments: list[str | None], needle: tuple[str, ...], spelled_from: int
) -> bool:
    """Whether `needle` is a run of `segments` reaching past the root's own segments."""
    width = len(needle)
    return any(
        tuple(segments[i : i + width]) == needle and i + width > spelled_from
        for i in range(len(segments) - width + 1)
    )


def inline_inventory_paths(source: str, module: Path | None = None) -> list[str]:
    """The `/` chains `source` builds to a path `lib.repo_paths` owns, each with its constant.

    A hit is a chain rooted in the repo whose string parts spell the inventory,
    `group_vars/all.yml`, `host_vars`, or the k3s role directory, its `files/` or its
    defaults, whether the parts are one string or several. The root is an anchor, a local name
    bound to one, or `__file__` arithmetic evaluated at `module`. A chain counts only for the
    segments it spells itself, so `K3S_ROLE / "tasks"` passes. Only the outermost chain is
    read, so one expression is one hit. A repo-relative STRING
    (`"ansible/inventory/group_vars/all.yml"`) is not a chain and not a hit: the deploy
    classifiers' tests hand those to the code under test as inputs.
    """
    tree = ast.parse(source)
    names = _local_anchors(tree, module)
    inner = {
        id(node.left)
        for node in ast.walk(tree)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
    }
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.BinOp) or id(node) in inner:
            continue
        chain = _chain(node, names, module)
        if chain is None:
            continue
        spelled_from, segments = chain
        # A module binding its own `INVENTORY` hides every chain built from it, so the binding
        # is a hit too: the two-step build is how #4139's review found one the row passed.
        if segments == ["ansible", "inventory"] and spelled_from < 2:
            found.append(f"{ast.unparse(node)} (import INVENTORY from lib.repo_paths)")
            continue
        found += [
            f"{ast.unparse(node)} (import {constant} from lib.repo_paths)"
            for needle, constant in _OWNED_PATHS
            if _spells(segments, needle, spelled_from)
        ][:1]
    return found
