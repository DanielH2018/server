"""Textual censuses of where the tracked Python sits, one `_row_table.Census` row each.

`test_census_rows_python.py` holds the rows that judge what a module says. A row here judges
which siblings a module sits beside, so its subject is a group of files rather than one, and
its selector returns `Family` subjects rather than paths.

Run: uv run pytest ansible/tests/repo/test_census_rows_layout.py
"""

import ast
from collections import defaultdict
from collections.abc import Iterable
from functools import cache
from pathlib import PurePosixPath

import pytest
from _row_table import Census, Subject, check, proof_problems, tracked
from lib.script_classify import _has_main_guard

# ── Flat module families ──────────────────────────────────────────────────────────────

FAMILY_SIZE = 3


class Family(Subject):
    """Flat sibling modules sharing a first or last `_`-word, keyed by the pattern they share.

    The key reads `<dir>/foo_*.py` for a shared first word, which covers `foo.py` itself, and
    `<dir>/*_foo.py` for a shared last word.
    """

    def __init__(self, rel: str, members: Iterable[Subject]):
        super().__init__(rel, text="")
        self.members = tuple(members)


def _is_flat(rel: str) -> bool:
    """Whether `rel` sits directly in a `scripts/` subdirectory or a role's `files/`.

    A module one directory deeper is already inside a package, which is the layout the row
    asks for.
    """
    parts = PurePosixPath(rel).parts
    if parts[0] == "scripts":
        return len(parts) == 3 and parts[1] != "tests"
    return len(parts) == 6 and parts[4] == "files"


@cache
def _families() -> tuple[Family, ...]:
    """Every group of FAMILY_SIZE or more flat siblings sharing a first or last word."""
    groups: dict[str, list[str]] = defaultdict(list)
    for rel in tracked("scripts/*.py", "ansible/roles/*/files/*.py"):
        if not _is_flat(rel):
            continue
        path = PurePosixPath(rel)
        words = path.stem.lstrip("_").split("_")
        groups[f"{path.parent}/{words[0]}_*.py"].append(rel)
        groups[f"{path.parent}/*_{words[-1]}.py"].append(rel)
    return tuple(
        Family(key, map(Subject, members))
        for key, members in sorted(groups.items())
        if len(members) >= FAMILY_SIZE
    )


def _is_main_guard(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "__name__"
    )


def _forwarded_module(member: Subject) -> str | None:
    """The module a forwarding shim hands its argv to, or None when `member` is not a shim.

    A consolidation leaves a shim at an old path that shipped code or an older checkout still
    runs (#4346, #4347). Its whole body is a docstring, imports and a `__main__` guard holding
    one statement: a call, optionally wrapped in `sys.exit`, of a name imported with `from`,
    whose arguments call nothing. Anything more is program logic.
    """
    try:
        tree = ast.parse(member.text)
    except SyntaxError:
        return None
    sources: dict[str, str | None] = {}
    guards: list[ast.If] = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            sources |= {alias.asname or alias.name: node.module for alias in node.names}
        elif isinstance(node, ast.If) and _is_main_guard(node):
            guards.append(node)
        elif not isinstance(node, ast.Import) and not (
            node is tree.body[0] and isinstance(node, ast.Expr)
        ):
            return None
    if len(guards) != 1 or guards[0].orelse or len(guards[0].body) != 1:
        return None
    stmt = guards[0].body[0]
    call = stmt.value if isinstance(stmt, ast.Expr) else None
    if (
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and ast.unparse(call.func) == "sys.exit"
        and len(call.args) == 1
    ):
        call = call.args[0]
    if not (
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id in sources
    ):
        return None
    if any(
        isinstance(inner, ast.Call)
        for arg in [*call.args, *call.keywords]
        for inner in ast.walk(arg)
    ):
        return None
    return sources[call.func.id]


def _family_offence(subject: Subject) -> list[str]:
    assert isinstance(subject, Family), subject.rel
    stems = {PurePosixPath(m.rel).stem: m for m in subject.members}
    targets = {m.rel: _forwarded_module(m) for m in subject.members}
    # A shim counts out only when it forwards to a real entrypoint in its own family. One that
    # forwards to a guard-less sibling is the family's entrypoint, however thin.
    real = {
        stem
        for stem, m in stems.items()
        if _has_main_guard(m.text) and targets[m.rel] is None
    }
    counted = [m for m in subject.members if targets[m.rel] not in real]
    entrypoints = [m for m in counted if _has_main_guard(m.text)]
    if len(counted) < FAMILY_SIZE or not entrypoints:
        return []
    names = ", ".join(PurePosixPath(m.rel).name for m in counted)
    mains = ", ".join(PurePosixPath(m.rel).name for m in entrypoints)
    return [f"{len(counted)} flat modules with entrypoint {mains} ({names})"]


_MAIN = 'def main():\n    return 0\n\n\nif __name__ == "__main__":\n    main()\n'
_SHIM = 'import sys\n\nfrom foo import main\n\nif __name__ == "__main__":\n    sys.exit(main(["a"]))\n'
_INLINE = 'import sys\n\nif __name__ == "__main__":\n    print(sys.argv)\n'
_THIN = 'import sys\n\nfrom foo_cli import main\n\nif __name__ == "__main__":\n    sys.exit(main())\n'
_IMPORTED_LOGIC = (
    'import json\nfrom pathlib import Path\n\nif __name__ == "__main__":\n'
    '    print(json.loads(Path("x").read_text()))\n'
)
_TYPE_CHECKING = (
    "from typing import TYPE_CHECKING\nfrom foo import main\n\nif TYPE_CHECKING:\n"
    '    main()\n\nif __name__ == "__main__":\n    main()\n'
)
_ARG_CALLS = 'import sys\n\nfrom foo import main\n\nif __name__ == "__main__":\n    sys.exit(main(sum([1])))\n'


def _fixture(key: str, **members: str) -> Family:
    parent = key.rsplit("/", 1)[0]
    return Family(
        key, (Subject(f"{parent}/{name}.py", text) for name, text in members.items())
    )


ROWS = (
    Census(
        name="module-families-live-in-a-package",
        reason=(
            "Three or more flat sibling modules sharing a first or last `_`-word, one of them "
            "an entrypoint, read as several programs where there is one. They live in a "
            "`<prefix>_lib/` package behind that one entrypoint, as `land_lib/` and "
            "`probe_lib/` do (docs/python-code-organization.md, the operator's 2026-10-10 "
            "ruling). The census reads the modules directly in a `scripts/` subdirectory or a "
            "role's `files/`; a module inside a package is grouped already. A forwarding shim "
            "left at an old path holds no logic and does not count. Move the libraries, or "
            "add the family's key to `allow` with the reason it stays flat."
        ),
        files=_families,
        offence=_family_offence,
        red=(
            _fixture("scripts/x/foo_*.py", foo_a=_MAIN, foo_b="", foo_c=""),
            _fixture("scripts/x/*_gates.py", a_gates=_MAIN, b_gates=_MAIN, c_gates=""),
            # Logic inline under the guard calls nothing imported, so it is no shim.
            _fixture("scripts/x/foo_*.py", foo=_INLINE, foo_a="", foo_b=""),
            # A thin entrypoint over guard-less flat libraries is the layout itself.
            _fixture(
                "scripts/x/foo_*.py",
                foo=_THIN,
                foo_cli="def main():\n    return 0\n",
                foo_util="X = 1\n",
            ),
            # Logic under the guard that calls an imported name, a second top-level `if`, or a
            # forwarding call whose argument calls something is not forwarding. Each of the last
            # two fixtures breaks one rule only, so either rule regressing turns its proof red.
            _fixture("scripts/x/foo_*.py", foo=_MAIN, foo_a=_IMPORTED_LOGIC, foo_b=""),
            _fixture("scripts/x/foo_*.py", foo=_MAIN, foo_a=_TYPE_CHECKING, foo_b=""),
            _fixture("scripts/x/foo_*.py", foo=_MAIN, foo_a=_ARG_CALLS, foo_b=""),
        ),
        green=(
            _fixture("scripts/x/foo_*.py", foo_a="X = 1\n", foo_b="", foo_c=""),
            _fixture("scripts/x/foo_*.py", foo=_MAIN, foo_a=_SHIM, foo_b=_SHIM),
        ),
        # Five groups on 2026-10-11, after #4416-#4418 moved the last three flat families.
        min_matches=5,
        # A suffix family, a role-`files/` family with an entrypoint, and monitor-bridge's
        # copies, a role-`files/` family with none.
        must_find=frozenset(
            {
                "scripts/validate/*_templates.py",
                "ansible/roles/setup/gitops_deploy/files/gitops_*.py",
                "ansible/roles/k8s/monitor-bridge/files/gitops_*.py",
            }
        ),
        allow={
            "scripts/validate/*_templates.py": (
                "each is the entry of its own prek hook, which runs it under its own `files` "
                "filter in prek.toml"
            ),
            "ansible/roles/setup/gitops_deploy/files/gitops_*.py": (
                "the `gitops_*` copies: `scripts/dev/gen_gitops_markers.py` copies "
                "gitops_hold, gitops_ledger and gitops_markers verbatim into monitor-bridge's "
                "flat `files/`, and deploy_ui and renovate_agent install them into /opt from "
                "these paths"
            ),
        },
    ),
)
_IDS = [row.name for row in ROWS]


@pytest.mark.parametrize("row", ROWS, ids=_IDS)
def test_census_row_holds_on_the_tree(row: Census):
    problems = check(row)
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("row", ROWS, ids=_IDS)
def test_census_row_flags_its_red_subjects_and_passes_its_green_ones(row: Census):
    assert not proof_problems(row)
