"""The AST detector behind the census row that keeps inventory paths in `lib.repo_paths`.

`test_census_rows_test_renders.py` runs `inline_inventory_paths` as the
`tests-take-inventory-paths-from-repo-paths` row over `pytest_only_modules()` (#4002). It sits
apart from `_render_helper_rules.py`, whose detectors fill that module to its 500-line cap. Like
them, it takes TEXT rather than a path, so a row's red and green subjects can be one-line
snippets.
"""

import ast

# The names a test module binds the repo root or an anchor under it to, by the spelling
# `local_repo_roots` and the `repo_paths` imports leave in the tree, each with the segments it
# stands for. A chain's segments start with those, so `INVENTORY / "hosts.ini"` spells the
# same path as `REPO / "ansible/inventory/hosts.ini"`.
_PATH_ANCHORS = {
    "REPO": (),
    "_REPO": (),
    "REPO_ROOT": (),
    "ANSIBLE": ("ansible",),
    "ROLES": ("ansible", "roles"),
    "INVENTORY": ("ansible", "inventory"),
}
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
    segments: list[str | None] = list(_PATH_ANCHORS[left.id])
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
        # A module binding its own `INVENTORY` hides every chain built from it, so the binding
        # is a hit too: the two-step build is how #4139's review found one the row passed.
        if segments == ["ansible", "inventory"]:
            found.append(f"{ast.unparse(node)} (import INVENTORY from lib.repo_paths)")
        found += [
            f"{ast.unparse(node)} (import {constant} from lib.repo_paths)"
            for needle, constant in _OWNED_PATHS
            if _spells(segments, needle)
        ]
    return found
