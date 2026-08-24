#!/usr/bin/env python3
"""Generate docs/reference/scripts.md — every first-party script and what it is for.

WHY THIS PAGE IS GENERATED. There are around 40 scripts in scripts/, and every one already
carries a module docstring that says what it does — several with a `Usage::` block. A
hand-written index of them is stale the day someone adds the forty-first. The docstrings
already ARE the documentation; this assembles them.

WHY IT PARSES AND NEVER IMPORTS. Reading a docstring by importing the module runs its
top-level code. Across this directory that would mean dialling hosts, taking locks and
resolving SOPS on every docs refresh. `ast.parse` plus `ast.get_docstring` reads the same
string and executes nothing. A fixture script whose body raises at module level pins that
in the tests.

WHAT IT REPORTS RATHER THAN HIDES. A script that does not parse, and a script with no
docstring, both get a row saying so. Dropping them would make the page quietly incomplete,
which is worse than a visible gap. The same goes for the test column: a script with no
`test_<name>.py` shows an empty cell, because an untested script is a fact worth surfacing.

WHAT IT CANNOT DECIDE. Whether a script is safe to run. The summary is whatever its author
wrote, and nothing here judges blast radius — `docs/reference/crons.md` does that for the
scheduled ones.

Usage::

    uv run python scripts/gen_reference_scripts.py --out docs/reference/scripts.md
"""

from __future__ import annotations

import argparse
import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"

# Not documentation about the tree: a test, a pytest fixture module, or a private helper
# whose name says it is not an entry point.
_EXCLUDED_PREFIXES = ("test_", "_")
_EXCLUDED_NAMES = {"conftest.py"}

_SUFFIXES = (".py", ".sh")

# The reStructuredText usage marker the repo's scripts already use, and the indented block
# that follows it.
_USAGE_RE = re.compile(r"^Usage::\s*$", re.MULTILINE)


def _python_docstring(path: Path) -> str | None:
    """The module docstring, or None if the file does not parse."""
    try:
        tree = ast.parse(path.read_text())
    except SyntaxError, ValueError, UnicodeDecodeError:
        return None
    return ast.get_docstring(tree) or ""


def _shell_docstring(path: Path) -> str:
    """The leading `#` comment block, shebang excluded."""
    lines = []
    for line in path.read_text().splitlines():
        if line.startswith("#!"):
            continue
        if line.startswith("#"):
            lines.append(line.lstrip("#").strip())
            continue
        if not line.strip() and not lines:
            continue
        break
    return "\n".join(lines)


def _usage(doc: str) -> str:
    """The indented block after a `Usage::` marker, dedented. Empty when absent."""
    match = _USAGE_RE.search(doc)
    if not match:
        return ""
    block = []
    for line in doc[match.end() :].splitlines():
        if not line.strip():
            if block:
                break
            continue
        if not line.startswith((" ", "\t")):
            break
        block.append(line.strip())
    return "\n".join(block)


def _is_candidate(path: Path) -> bool:
    return (
        path.is_file()
        and path.suffix in _SUFFIXES
        and path.name not in _EXCLUDED_NAMES
        and not path.name.startswith(_EXCLUDED_PREFIXES)
    )


def build_rows(scripts: Path = SCRIPTS) -> list[dict[str, str]]:
    """One row per first-party script, sorted by name."""
    rows = []
    for path in sorted(scripts.glob("*")):
        if not _is_candidate(path):
            continue

        if path.suffix == ".py":
            doc = _python_docstring(path)
            if doc is None:
                summary, usage = f"({path.name} could not be parsed)", ""
            elif not doc.strip():
                summary, usage = "(no module docstring)", ""
            else:
                summary, usage = doc.strip().splitlines()[0].strip(), _usage(doc)
        else:
            doc = _shell_docstring(path)
            summary = (
                doc.splitlines()[0].strip() if doc.strip() else "(no leading comment)"
            )
            usage = _usage(doc)

        test = scripts / f"test_{path.stem}.py"
        rows.append(
            {
                "name": path.name,
                "summary": summary,
                "usage": usage,
                "tests": test.name if test.is_file() else "",
            }
        )
    return rows


def _md_cell(value: str) -> str:
    """A literal pipe in a summary adds a column silently — the table still renders, wrong."""
    return value.replace("|", "\\|")


def render_markdown(rows: list[dict[str, str]]) -> str:
    from docs_provenance import generated_banner

    untested = [r for r in rows if not r["tests"]]

    parts = [generated_banner("scripts/gen_reference_scripts.py")]
    parts.append("# Scripts\n")
    parts.append(
        f"{len(rows)} first-party script(s) in `scripts/`. Each summary is the script's own "
        "module docstring — change the docstring to change this page.\n"
    )
    parts.append(
        '!!! note "What this page does not tell you"\n'
        "    Whether a script is safe to run. The summary is whatever its author wrote, and "
        "nothing here judges blast radius. For the ones that run unattended, and which of "
        "those change state, see [Scheduled jobs](crons.md).\n"
    )
    parts.append(
        f"\n**{len(untested)} of {len(rows)} have no test file.** That is not automatically "
        "wrong — a thin wrapper round another tool may not need one — but it is the list to "
        "read before trusting a script you have not run.\n"
    )

    parts.append("\n## The scripts\n")
    parts.append("| Script | What it does | Tests |")
    parts.append("|---|---|---|")
    for row in rows:
        test = f"`{row['tests']}`" if row["tests"] else "—"
        parts.append(f"| `{row['name']}` | {_md_cell(row['summary'])} | {test} |")

    documented = [r for r in rows if r["usage"]]
    parts.append(
        f"\n## Usage\n\n{len(documented)} script(s) document how to invoke themselves. "
        "The rest take `--help`.\n"
    )
    for row in documented:
        parts.append(f"\n### `{row['name']}`\n")
        parts.append("```")
        parts.append(row["usage"])
        parts.append("```")

    return "\n".join(parts).rstrip("\n") + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True, help="output file path")
    parser.add_argument("--scripts", type=Path, default=SCRIPTS)
    args = parser.parse_args(argv)

    from docs_provenance import write_if_body_changed

    rows = build_rows(args.scripts)
    wrote = write_if_body_changed(args.out, render_markdown(rows))
    print(
        f"gen_reference_scripts: {len(rows)} script(s), "
        f"{'wrote' if wrote else 'unchanged'} {args.out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
