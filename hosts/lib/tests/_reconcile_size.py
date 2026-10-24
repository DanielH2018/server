"""Count the source lines of `hosts/lib/homelab-reconcile` and of the modules only it imports.

Exit observation 7 of `docs/target-architecture/host/h7-delete-ansible.md` bounds the host
plane's second mechanism by this count. A source line is a line holding at least one token
that is not a comment, and that is not part of a docstring. Blank lines, comments and docstrings
do not count, and tests are not counted because they live under `hosts/lib/tests/`.

A module counts when reconcile imports it, directly or through another counted module, and no
other program under `hosts/lib/` imports it. Reconcile's own imports select the modules, never
a glob over `hosts/lib/*.py`, because other programs keep modules there. A module that a second
program imports is shared code, so it belongs to neither program's budget. Splitting reconcile
into helper modules therefore cannot hide growth, and adding a shared module cannot inflate it.
"""

import ast
import io
import tokenize
from pathlib import Path

RECONCILE = "homelab-reconcile"
_NOT_SOURCE = frozenset(
    {
        tokenize.COMMENT,
        tokenize.NL,
        tokenize.NEWLINE,
        tokenize.INDENT,
        tokenize.DEDENT,
        tokenize.ENDMARKER,
        tokenize.ENCODING,
    }
)


def source_lines(text: str) -> int:
    """The lines of `text` that hold code: no blank line, comment line or docstring line."""
    docstrings: set[int] = set()
    for node in ast.walk(ast.parse(text)):
        if not isinstance(
            node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        ):
            continue
        first = node.body[0] if node.body else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            docstrings.update(range(first.lineno, (first.end_lineno or 0) + 1))
    code: set[int] = set()
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type not in _NOT_SOURCE:
            code.update(range(token.start[0], token.end[0] + 1))
    return len(code - docstrings)


def _imported_modules(text: str) -> set[str]:
    """Every dotted module name `text` imports, at module level or inside a function."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def _file_of(lib: Path, dotted: str) -> list[Path]:
    """The files under `lib` that `import dotted` loads: the module and its parent packages."""
    parts = dotted.split(".")
    found = []
    for end in range(1, len(parts) + 1):
        stem = lib.joinpath(*parts[:end])
        for candidate in (stem.with_suffix(".py"), stem / "__init__.py"):
            if candidate.is_file():
                found.append(candidate)
                break
    return found


def _programs(lib: Path) -> list[Path]:
    """Every Python program or module under `lib`, tests excluded."""
    found = []
    for path in sorted(lib.rglob("*")):
        if not path.is_file() or "tests" in path.relative_to(lib).parts:
            continue
        if path.suffix == ".py" or path.read_bytes().startswith(b"#!/usr/bin/python3"):
            found.append(path)
    return found


def _imports_of(lib: Path, path: Path) -> set[Path]:
    """The files under `lib` that `path` imports."""
    files: set[Path] = set()
    for dotted in _imported_modules(path.read_text()):
        files.update(_file_of(lib, dotted))
    return files - {path}


def counted_files(lib: Path) -> list[Path]:
    """Reconcile, then the modules under `lib` that only reconcile imports."""
    programs = _programs(lib)
    reachable: set[Path] = set()
    pending = [lib / RECONCILE]
    while pending:
        for path in sorted(_imports_of(lib, pending.pop())):
            if path not in reachable:
                reachable.add(path)
                pending.append(path)
    others: set[Path] = set()
    for path in programs:
        if path != lib / RECONCILE and path not in reachable:
            others |= _imports_of(lib, path)
    shared = set(reachable & others)
    pending = list(shared)
    while pending:
        for imported in _imports_of(lib, pending.pop()):
            if imported not in shared:
                shared.add(imported)
                pending.append(imported)
    return [lib / RECONCILE, *sorted(reachable - shared)]


def census(lib: Path) -> dict[str, int]:
    """Each counted file, relative to `lib`, mapped to its source lines."""
    return {
        str(path.relative_to(lib)): source_lines(path.read_text())
        for path in counted_files(lib)
    }
