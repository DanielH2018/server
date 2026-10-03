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
which is worse than a visible gap.

HOW EACH SCRIPT IS RUN IS DERIVED, NOT DECLARED. A hand-kept list of "these ones are
automated" is stale the first time someone adds a cron. The tree already says how every
script is reached: `prek.toml` names the commit gates, `ansible.builtin.cron` names the
scheduled ones, the workflows name the CI ones, and the import graph names the modules that
are libraries rather than entry points. `lib.script_classify.classify()` reads those, so
the page cannot drift from the tree. The classifier is `lib/script_classify.py`; this file
assembles its answers into the page.

THE TESTS COLUMN NAMES ONLY A DIRECT TEST. A `test_<name>.py` in the script's `tests/` sibling
or beside it counts; nothing else does. An earlier version also credited any test that
imported the script or named its path, and keeping that judgement honest took a module and a
suite of its own for a column nobody acted on (#3283). An empty cell therefore means "no test
named after it", not "nothing exercises it".

WHAT IT CANNOT DECIDE. Whether a script is safe to run. The summary is whatever its author
wrote, and nothing here judges blast radius — `docs/reference/crons.md` does that for the
scheduled ones.

Usage::

    uv run python scripts/docs/reference/scripts.py --out docs/reference/scripts.md
"""

import argparse
import ast
import re
from pathlib import Path


# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from lib.docs_provenance import md_cell as _md_cell
from lib.exit_codes import CONTRACTS as _EXIT_CONTRACTS, contract as _exit_contract
from lib.repo_paths import REPO, SCRIPTS
from lib.script_classify import RUNS, candidates, classify

# The reStructuredText usage marker the repo's scripts already use, and the indented block
# that follows it.
_USAGE_RE = re.compile(r"^Usage::\s*$", re.MULTILINE)

# The Directory cell of a script that sits directly in scripts/ rather than in a subdirectory.
TOP_LEVEL = "(top level)"


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


def _exit_codes_cell(rel_path: str) -> str:
    """The codes `rel_path` declares in `lib.exit_codes`, or "" when it declares none.

    Only the numbers, linked to the per-script table below: a meaning per code does not fit a
    row, and the point of the column is to say at a glance whether a non-zero exit from this
    script means anything more than "it failed".
    """
    codes = _exit_contract(rel_path)
    if not codes:
        return ""
    anchor = re.sub(r"[^a-z0-9]", "", rel_path.lower())
    return f"[{', '.join(str(c.value) for c in codes)}](#{anchor})"


def build_rows(scripts: Path = SCRIPTS, repo: Path = REPO) -> list[dict[str, str]]:
    """One row per first-party script, sorted by name."""
    verdicts = classify(repo, scripts)
    rows = []
    for path in candidates(scripts):
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
            # The first NON-EMPTY line: two of these scripts open `#!`, then a bare `#`,
            # then the sentence. Taking line one left them with a blank summary cell.
            lines = [line for line in doc.splitlines() if line.strip()]
            summary = lines[0].strip() if lines else "(no leading comment)"
            # Two of them open "name.sh — what it does"; the name is already the row label.
            summary = re.sub(rf"^{re.escape(path.name)}\s+[—-]\s*", "", summary)
            usage = _usage(doc)

        # The split layout keeps a script's test in a sibling `tests/`; the flat one beside it.
        direct = path.parent / "tests" / f"test_{path.stem}.py"
        if not direct.is_file():
            direct = path.parent / f"test_{path.stem}.py"
        test = direct.name if direct.is_file() else ""
        run, evidence = verdicts.get(
            path.name, ("adhoc", "no automated caller in the tree")
        )
        # The top-level subdirectory only: `docs/reference/x.py` files under `docs`, which is
        # the grouping the Directory filter on the page offers.
        within = path.relative_to(scripts).parts
        rows.append(
            {
                "name": path.name,
                "path": str(path.relative_to(scripts.parent)),
                "directory": within[0] if len(within) > 1 else TOP_LEVEL,
                "summary": summary,
                "usage": usage,
                "tests": test,
                "run": run,
                "evidence": evidence,
                "exit_codes": _exit_codes_cell(str(path.relative_to(scripts.parent))),
            }
        )
    return rows


def render_markdown(rows: list[dict[str, str]]) -> str:
    """Render `rows` as the "Scripts" reference page, grouped by how each script is run.

    Splits the rows into scheduled / gate / library / adhoc sections and appends a usage block
    for each script that documents its own invocation.

    Args:
        rows: Script rows as returned by `build_rows`.

    Returns:
        The full page as Markdown text, ending in a single trailing newline.
    """
    from lib.docs_provenance import generated_banner

    by_run = {kind: [r for r in rows if r["run"] == kind] for kind in RUNS}

    parts = [generated_banner("scripts/docs/reference/scripts.py")]
    parts.append("# Scripts\n")
    parts.append(
        f"{len(rows)} first-party script(s) in `scripts/`. Each summary is the script's own "
        "module docstring — change the docstring to change this page.\n"
    )
    parts.append(
        "The sections below split them by **how each one is run**, which is derived from the "
        "tree rather than declared: a cron `job:`, a `prek.toml` entry, a workflow step, a "
        "Claude hook, an Ansible task, or an import edge. The *Reached by* column is the "
        "evidence, so a wrong answer is a wrong answer about a real file. The filter bar "
        "above the first table narrows all four at once: by section, by directory, or by any "
        "text in a row. A header click sorts by that column.\n"
    )
    parts.append(
        '!!! note "What this page does not tell you"\n'
        "    Whether a script is safe to run. The summary is whatever its author wrote, and "
        "nothing here judges blast radius. For the ones that run unattended, and which of "
        "those change state, see [Scheduled jobs](crons.md).\n"
    )
    parts.append(
        '!!! note "Where the Tests column looks"\n'
        "    Only for a `test_<name>.py` in the script's `tests/` sibling or beside it. A script "
        "with an empty cell may still be exercised elsewhere: `gitops_tick.sh` has five tests "
        "in `test_gitops_manual_trigger.py`, and a module split out of a facade is run by the "
        "facade's suite. The column says where a script's own suite lives, not whether "
        "anything reaches it.\n"
    )
    for kind, heading in (
        ("scheduled", "Run automatically, on a schedule"),
        ("gate", "Run automatically, on a commit, CI run, deploy or session"),
        ("library", "Imported, never run on their own"),
        ("adhoc", "Run by hand"),
    ):
        section = by_run[kind]
        parts.append(f"\n## {heading}\n")
        parts.append(f"{len(section)} script(s) — {RUNS[kind]}.\n")
        if not section:
            parts.append("None.\n")
            continue
        # Script stays the first cell: _mkdocs_repo_links.py finds each row's anchor target
        # there. Directory repeats a prefix of the path so the page's table filter
        # (docs/assets/table-filter.js) can offer it as a choice.
        parts.append(
            "| Script | Directory | What it does | Reached by | Tests | Exit codes |"
        )
        parts.append("|---|---|---|---|---|---|")
        for row in section:
            test = f"`{row['tests']}`" if row["tests"] else "—"
            parts.append(
                f"| `{row['path']}` | {row['directory']} | {_md_cell(row['summary'])} | "
                f"{_md_cell(row['evidence'])} | {test} | {row['exit_codes'] or '—'} |"
            )

    parts.append("\n## Exit codes\n")
    parts.append(
        f"{len(_EXIT_CONTRACTS)} entry point(s) declare a contract in "
        "`scripts/lib/exit_codes.py`, which is where these tables are rendered from. Every "
        "other script here exits 0 or non-zero and says nothing more; 64 is a usage error and "
        "75 a temporary failure wherever they appear, because each family names the same "
        "spine.\n"
    )
    for entry_point, codes in _EXIT_CONTRACTS.items():
        parts.append(f"\n### `{entry_point}`\n")
        parts.append("| Exit | Name | Meaning | What to do |")
        parts.append("|---|---|---|---|")
        for code in codes:
            parts.append(
                f"| {code.value} | `{code.const}` | {_md_cell(code.meaning)} | "
                f"{_md_cell(code.remedy) or '—'} |"
            )

    documented = [r for r in rows if r["usage"]]
    parts.append(
        f"\n## Usage\n\n{len(documented)} script(s) document how to invoke themselves. "
        "The rest take `--help`, which every catalogued entry point answers with exit 0 "
        "(`scripts/lib/tests/test_entry_points_answer_help.py`).\n"
    )
    for row in documented:
        parts.append(f"\n### `{row['path']}`\n")
        parts.append("```")
        parts.append(row["usage"])
        parts.append("```")

    return "\n".join(parts).rstrip("\n") + "\n"


def main(argv: list[str] | None = None) -> int:
    """Build the script rows, render the reference page, and write it if the body changed.

    Returns:
        The exit code from `finish_generator` (0 on success, non-zero on a write failure).
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True, help="output file path")
    parser.add_argument("--scripts", type=Path, default=SCRIPTS)
    args = parser.parse_args(argv)

    from lib.docs_provenance import finish_generator

    rows = build_rows(args.scripts)
    return finish_generator(
        "docs.reference.scripts", args.out, rows, render_markdown, "script"
    )


if __name__ == "__main__":
    raise SystemExit(main())
