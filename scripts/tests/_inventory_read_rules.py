"""The AST detector behind the census row that sends every read of the inventory through `lib.estate`.

ADR-0042 moves the inventory's files (`docs/variable-layout-migration.md`), so a module that
opens one by path breaks at the move and reads nothing after it. A read is a call that takes an
inventory path, or a path under it, as its receiver or an argument: `load_yaml(ALL_VARS)`,
`HOST_VARS.glob("*.yml")`, `vars_of(HOST_VARS / "daniel-pi.yml")`. A call that only names the path,
such as `relative_to` or `shutil.copytree`, is not a read. The path resolves through
`_inventory_path_rules`, so a name a module binds to `INVENTORY / "group_vars" / "all.yml"` counts
as the constant does.
"""

import ast
from pathlib import Path

from _inventory_path_rules import _chain, _local_anchors
from _row_table import parse

# Calls that take a path without opening it.
_NOT_READS = frozenset(
    {
        "as_posix",
        "copy",
        "copy2",
        "copy_host_files",
        "copytree",
        "exists",
        "host_vars_file",
        "is_dir",
        "is_file",
        "is_relative_to",
        "relative_to",
        "resolve",
        "str",
        "symlink_to",
    }
)


def _name(func: ast.expr) -> str:
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def _inventory(node: ast.expr, names: dict, module: Path | None) -> bool:
    """Whether `node` is a path in the inventory, spelled or bound.

    The hosts directory is an inventory path through the `HOST_VARS` name or a spelled
    `hosts/<host>/vars.yml`, because the rest of `hosts/` is not inventory.
    """
    if isinstance(node, ast.Name):
        return names.get(node.id, ())[:1] == ("inventory",) or (
            node.id == "HOST_VARS" and names.get(node.id) == ("hosts",)
        )
    if isinstance(node, ast.Attribute) and node.attr in ("parent",):
        return _inventory(node.value, names, module)
    chain = _chain(node, names, module) if isinstance(node, ast.BinOp) else None
    if chain is None:
        return False
    segments = chain[1]
    return segments[:1] == ["inventory"] or (
        segments[:1] == ["hosts"] and segments[-1] == "vars.yml"
    )


def inventory_reads(source: str, module: Path | None = None) -> list[str]:
    """Each call in `source` that reads an inventory file, as `line N: <call>`.

    A parameter whose default is an inventory path counts too, because every caller that omits
    it reads the file through the function.
    """
    tree = parse(source)
    names = _local_anchors(tree, module, {})
    # A parameter shadows a module name, as `def f(host_vars: dict)` does `Inventory.host_vars`.
    for node in ast.walk(tree):
        if isinstance(node, ast.arg):
            names.pop(node.arg, None)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defaults = [*node.args.defaults, *filter(None, node.args.kw_defaults)]
            if any(_inventory(d, names, module) for d in defaults):
                found.append(f"line {node.lineno}: def {node.name} defaults to it")
            continue
        if not isinstance(node, ast.Call) or _name(node.func) in _NOT_READS:
            continue
        receiver = node.func.value if isinstance(node.func, ast.Attribute) else None
        operands = [*node.args, *(k.value for k in node.keywords)]
        if receiver is not None:
            operands.append(receiver)
        if any(_inventory(arg, names, module) for arg in operands):
            found.append(f"line {node.lineno}: {ast.unparse(node)[:80]}")
    return found
