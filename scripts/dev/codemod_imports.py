"""Qualify an area's bare sibling imports and delete the `sys.path` inserts that backed them.

Slice 3 of the scripts packaging milestone runs this once per area under `scripts/`, and slice 4
deletes it (docs/adr/0039-scripts-is-installed-in-place.md). It works in two passes.

`--qualify` rewrites each import of a module or package directly under `scripts/<area>/` to its
dotted name, in every tracked Python file under `scripts/`, `tests/`, `hosts/tests/`,
`.claude/hooks/tests/` and `evals/`: `from X import a` becomes `from <area>.X import a`, and
`import X as y` becomes `from <area> import X as y`. A module name inside a string, such as a
`monkeypatch.setattr` target, needs a hand.

Then it removes each module-level `sys.path.insert(0, str(Path(__file__).resolve().parents[N]))`
whose target is `scripts/`, or the area's own directory once `--qualify` ran, with the comment
block directly above it. Ruff drops the `sys` and `Path` imports and the `# noqa: E402` markers
the removal leaves unused. An insert of any other directory stays, and the count line says how
many: `hosts/lib` goes through `lib.host_modules`, and the rest need a hand.

A comment block that holds a `DECIDED:` marker stays where it is, because an ADR may cite it.

Typical usage example:

  uv run python scripts/dev/codemod_imports.py dev --qualify
"""

import ast
import re
import subprocess
import sys
from pathlib import Path

from lib.cli_help import ArgumentParser
from lib.git import git_stdout
from lib.repo_paths import REPO, SCRIPTS

_PARENTS = re.compile(
    r"^(?:str\()?_?Path\(__file__\)\.resolve\(\)\.parent(?:s\[(\d+)\])?\)?$"
)
_IMPORTERS = ("scripts", "tests", "hosts/tests", ".claude/hooks/tests", "evals")


def _is_insert(node: ast.stmt) -> ast.Call | None:
    if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
        return None
    call = node.value
    func = call.func
    is_path_call = (
        isinstance(func, ast.Attribute)
        and func.attr in {"insert", "append"}
        and ast.unparse(func.value) in {"sys.path", "_sys.path"}
    )
    return call if is_path_call and call.args else None


def _target(call: ast.Call, path: Path) -> Path | None:
    match = _PARENTS.match(ast.unparse(call.args[-1]))
    if not match:
        return None
    parents = path.resolve().parents
    depth = int(match.group(1) or 0)
    return parents[depth] if depth < len(parents) else None


def _comment_start(lines: list[str], first: int) -> int:
    """The 0-based index where the comment block ending just above line `first` starts."""
    start = first
    while start > 0 and lines[start - 1].lstrip().startswith("#"):
        start -= 1
    block = lines[start:first]
    return first if any("DECIDED:" in line for line in block) else start


def strip_file(path: Path, dead: frozenset[Path]) -> tuple[int, int]:
    """Remove `path`'s module-level inserts of a directory in `dead`.

    Returns:
        (inserts removed, inserts of another directory kept).
    """
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    spans: list[tuple[int, int]] = []
    kept = 0
    resolved = {d.resolve() for d in dead}
    for node in ast.parse(text).body:
        call = _is_insert(node)
        if call is None:
            continue
        if _target(call, path) not in resolved:
            kept += 1
            continue
        first = node.lineno - 1
        spans.append((_comment_start(lines, first), node.end_lineno or node.lineno))
    for start, end in reversed(spans):
        del lines[start:end]
    if spans:
        path.write_text("".join(lines), encoding="utf-8")
    return len(spans), kept


def area_names(area_dir: Path) -> frozenset[str]:
    """The modules and packages directly under `area_dir`, as a bare import names them."""
    modules = {p.stem for p in area_dir.glob("*.py")}
    packages = {
        p.name
        for p in area_dir.iterdir()
        if p.is_dir()
        and p.name not in {"tests", "__pycache__"}
        and any(p.rglob("*.py"))
    }
    return frozenset(modules | packages)


def _qualified(
    node: ast.Import | ast.ImportFrom, area: str, names: frozenset[str]
) -> str | None:
    """The replacement for `node`'s leading `from X` or `import X`, or None to leave it.

    Only the statement's head changes, so a parenthesised name list and its comments survive.
    """
    if isinstance(node, ast.ImportFrom):
        if node.level or not node.module or node.module.split(".")[0] not in names:
            return None
        return f"from {area}.{node.module}"
    ours = [a for a in node.names if a.name.split(".")[0] in names]
    if not ours:
        return None
    if len(node.names) != 1 or "." in ours[0].name:
        raise ValueError(f"line {node.lineno}: {ast.unparse(node)} needs a hand")
    return f"from {area} import"


_HEAD = re.compile(r"from\s+[\w.]+|import")


def qualify_file(path: Path, area: str, names: frozenset[str]) -> int:
    """Rewrite `path`'s bare imports of `names` under `area`; return how many changed."""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    edits = []
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Import | ast.ImportFrom):
            head = _qualified(node, area, names)
            if head is not None:
                edits.append((node.lineno, node.col_offset, head))
    for lineno, col, head in sorted(edits, reverse=True):
        line = lines[lineno - 1]
        match = _HEAD.match(line, col)
        if match is None:
            raise ValueError(f"{path}:{lineno}: no import head at column {col}")
        lines[lineno - 1] = line[:col] + head + line[match.end() :]
    if edits:
        path.write_text("".join(lines), encoding="utf-8")
    return len(edits)


def _importers() -> list[Path]:
    listed = git_stdout(
        "ls-files", "-z", "--", *(f"{d}/*.py" for d in _IMPORTERS), cwd=REPO
    )
    return [REPO / rel for rel in listed.split("\0") if rel]


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("area", help="a directory under scripts/, such as lib or dev")
    parser.add_argument(
        "--qualify",
        action="store_true",
        help="also dot the area's bare sibling imports repo-wide and delete its own-dir inserts",
    )
    args = parser.parse_args(argv)
    root = SCRIPTS / args.area
    if not root.is_dir():
        parser.error(f"scripts/{args.area} is not a directory")
    changed: set[Path] = set()
    qualified = 0
    dead = frozenset({SCRIPTS})
    if args.qualify:
        names = area_names(root)
        for path in _importers():
            if count := qualify_file(path, args.area, names):
                qualified += count
                changed.add(path)
        dead |= {root}
    removed = kept = 0
    for path in sorted(root.rglob("*.py")):
        gone, other = strip_file(path, dead)
        removed += gone
        kept += other
        if gone:
            changed.add(path)
    if changed:
        files = [str(p) for p in sorted(changed)]
        ruff = [sys.executable, "-m", "ruff"]
        # `--fixable`, not `--select`: with only these two rules on, RUF100 reads every other
        # code's `noqa` as unused and deletes it.
        fix = ["check", "--fix", "--fixable", "F401,RUF100", "--quiet", *files]
        subprocess.run([*ruff, *fix], check=False)
        subprocess.run([*ruff, "format", "--quiet", *files], check=True)
    print(
        f"scripts/{args.area}: qualified {qualified} import(s), removed {removed} dead "
        f"insert(s), changed {len(changed)} file(s); {kept} insert(s) of another directory kept"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
