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


def _is_forwarding_shim(member: Subject) -> bool:
    """Whether the module only runs a function it imported, under its `__main__` guard.

    A consolidation leaves one at an old path that shipped code or an older checkout still runs
    (#4346, #4347). It defines no function or class, so it holds nothing to navigate to.
    """
    if not _has_main_guard(member.text):
        return False
    tree = ast.parse(member.text)
    if any(
        isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        for node in tree.body
    ):
        return False
    imported = {
        alias.asname or alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    return any(
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id in imported
        for guard in tree.body
        if isinstance(guard, ast.If)
        for call in ast.walk(guard)
    )


def _family_offence(subject: Subject) -> list[str]:
    assert isinstance(subject, Family), subject.rel
    counted = [m for m in subject.members if not _is_forwarding_shim(m)]
    entrypoints = [m for m in counted if _has_main_guard(m.text)]
    if len(counted) < FAMILY_SIZE or not entrypoints:
        return []
    names = ", ".join(PurePosixPath(m.rel).name for m in counted)
    mains = ", ".join(PurePosixPath(m.rel).name for m in entrypoints)
    return [f"{len(counted)} flat modules with entrypoint {mains} ({names})"]


_MAIN = 'def main():\n    return 0\n\n\nif __name__ == "__main__":\n    main()\n'
_SHIM = 'import sys\n\nfrom foo import main\n\nif __name__ == "__main__":\n    sys.exit(main(["a"]))\n'
_INLINE = 'import sys\n\nif __name__ == "__main__":\n    print(sys.argv)\n'


def _fixture(key: str, **members: str) -> Family:
    parent = key.rsplit("/", 1)[0]
    return Family(
        key, (Subject(f"{parent}/{name}.py", text) for name, text in members.items())
    )


# The families still flat on 2026-10-11 that the census behind #4344-#4355 missed. Each entry
# is removed in the PR that moves its libraries, because the harness reports it stale from then.
_PENDING = {
    "ansible/roles/k8s/configarr/files/configarr_*.py": 4416,
    "ansible/roles/setup/deploy_ui/files/deploy_*.py": 4417,
    "scripts/deploy_tools/setup_*.py": 4418,
}

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
        ),
        green=(
            _fixture("scripts/x/foo_*.py", foo_a="X = 1\n", foo_b="", foo_c=""),
            _fixture("scripts/x/foo_*.py", foo=_MAIN, foo_a=_SHIM, foo_b=_SHIM),
        ),
        # Eight groups on 2026-10-11; five stay once the _PENDING three move.
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
            **{
                key: f"pending consolidation: #{issue}"
                for key, issue in _PENDING.items()
            },
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
