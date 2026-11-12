#!/usr/bin/env python3
"""Run actionlint over a copy of `.github/` whose `uses: $/` refs read `uses: ./`.

Callers reach the shared composite action as `uses: $/.github/actions/setup`, the form that
pins it to the workflow's own commit. actionlint 1.7.12 rejects that form as "ref is
missing" and then skips the step, so it never checks the caller's `with:` keys against the
action's inputs (rhysd/actionlint#711). Rewritten to `./`, the same step resolves the local
action again and gets every check a `./` caller gets.

The rewrite keeps every reported position true for the real file: `$/` and `./` are both two
characters, and the copy keeps the repo-relative paths. actionlint finds its project root from
`.git`, so the copy gets a `git init`. Reading the rewritten text from stdin
(`-stdin-filename`) is no substitute, because stdin mode never resolves a local action.

The wrapper fails when no `$/` ref is left to rewrite: by then the wrapper is dead weight, and
the `actionlint` hook in `prek.toml` should call actionlint directly again.

Run via the ``actionlint`` prek hook, which passes the changed workflow paths. actionlint's
output and exit code pass through unchanged.
"""

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from lib.git import git
from lib.repo_paths import REPO

DOLLAR_REF = re.compile(r"""(\buses:[ \t]*["']?)\$/""")
WORKFLOWS = Path(".github/workflows")
COPIED = (WORKFLOWS, Path(".github/actions"), Path(".github/actionlint.yaml"))


def copy_rewritten(root: Path, dest: Path) -> int:
    """Copy `root`'s `.github/` inputs under `dest`, rewriting `$/` refs; return how many."""
    for rel in COPIED:
        source = root / rel
        if source.is_dir():
            shutil.copytree(source, dest / rel)
        elif source.is_file():
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest / rel)
    rewritten = 0
    for path in sorted((dest / WORKFLOWS).glob("*.y*ml")):
        text, count = DOLLAR_REF.subn(r"\1./", path.read_text(encoding="utf-8"))
        if count:
            path.write_text(text, encoding="utf-8")
            rewritten += count
    return rewritten


def lint(args: list[str], root: Path = REPO) -> int:
    """Run actionlint with `args` over the rewritten copy of `root`; return its exit code."""
    with tempfile.TemporaryDirectory(prefix="actionlint-") as tmp:
        dest = Path(tmp)
        if not copy_rewritten(root, dest):
            print(
                f"[FAIL] {WORKFLOWS}: no `uses: $/` ref is left to rewrite. Point the "
                f"`actionlint` hook in prek.toml back at `uv run --frozen actionlint` and "
                f"delete scripts/validate/actionlint_dollar_refs.py and its test.",
                file=sys.stderr,
            )
            return 1
        git("init", "-q", cwd=dest)
        return subprocess.run(["actionlint", *args], cwd=dest, check=False).returncode


if __name__ == "__main__":
    sys.exit(lint(sys.argv[1:]))
