"""Delete an area's `sys.path` inserts of `scripts/`, which the installed import root supplies.

Slice 3 of the scripts packaging milestone runs this once per area under `scripts/`, and slice 4
deletes it (docs/adr/0039-scripts-is-installed-in-place.md). It removes each module-level
`sys.path.insert(0, str(Path(__file__).resolve().parents[N]))` whose target is `scripts/`, with
the comment block directly above it, then lets ruff drop the `sys` and `Path` imports and the
`# noqa: E402` markers the removal leaves unused. An insert of any other directory stays, and
the count line says how many: `hosts/lib` goes through `lib.host_modules`, and the rest need a
hand.

A comment block that holds a `DECIDED:` marker stays where it is, because an ADR may cite it.

Typical usage example:

  uv run python scripts/dev/codemod_imports.py lib
"""

import ast
import re
import subprocess
import sys
from pathlib import Path

from lib.cli_help import ArgumentParser
from lib.repo_paths import SCRIPTS

_PARENTS = re.compile(
    r"^(?:str\()?_?Path\(__file__\)\.resolve\(\)\.parents\[(\d+)\]\)?$"
)


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


def _targets_scripts(call: ast.Call, path: Path, scripts: Path) -> bool:
    match = _PARENTS.match(ast.unparse(call.args[-1]))
    if not match:
        return False
    parents = path.resolve().parents
    depth = int(match.group(1))
    return depth < len(parents) and parents[depth] == scripts.resolve()


def _comment_start(lines: list[str], first: int) -> int:
    """The 0-based index where the comment block ending just above line `first` starts."""
    start = first
    while start > 0 and lines[start - 1].lstrip().startswith("#"):
        start -= 1
    block = lines[start:first]
    return first if any("DECIDED:" in line for line in block) else start


def strip_file(path: Path, scripts: Path = SCRIPTS) -> tuple[int, int]:
    """Remove `path`'s module-level inserts of `scripts`; return (removed, other inserts kept)."""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    spans: list[tuple[int, int]] = []
    kept = 0
    for node in ast.parse(text).body:
        call = _is_insert(node)
        if call is None:
            continue
        if not _targets_scripts(call, path, scripts):
            kept += 1
            continue
        first = node.lineno - 1
        spans.append((_comment_start(lines, first), node.end_lineno or node.lineno))
    for start, end in reversed(spans):
        del lines[start:end]
    if spans:
        path.write_text("".join(lines), encoding="utf-8")
    return len(spans), kept


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("area", help="a directory under scripts/, such as lib or dev")
    args = parser.parse_args(argv)
    root = SCRIPTS / args.area
    if not root.is_dir():
        parser.error(f"scripts/{args.area} is not a directory")
    changed: list[Path] = []
    removed = kept = 0
    for path in sorted(root.rglob("*.py")):
        gone, other = strip_file(path)
        removed += gone
        kept += other
        if gone:
            changed.append(path)
    if changed:
        files = [str(p) for p in changed]
        ruff = [sys.executable, "-m", "ruff"]
        # `--fixable`, not `--select`: with only these two rules on, RUF100 reads every other
        # code's `noqa` as unused and deletes it.
        fix = ["check", "--fix", "--fixable", "F401,RUF100", "--quiet", *files]
        subprocess.run([*ruff, *fix], check=False)
        subprocess.run([*ruff, "format", "--quiet", *files], check=True)
    print(
        f"scripts/{args.area}: removed {removed} insert(s) of scripts/ from "
        f"{len(changed)} file(s); {kept} insert(s) of another directory kept"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
