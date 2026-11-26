"""Textual censuses of where the tracked Python sits, one `_row_table.Census` row each.

`test_census_rows_python.py` holds the rows that judge what a module says. A row here judges
which siblings a module sits beside, so its subject is a group of files rather than one, and
its selector returns `Family` subjects rather than paths. The next row judges which import
root a module reaches its siblings through. One row judges how the test modules are grouped. The last
judges which module owns the review pipeline's `Runner` alias.

Run: uv run pytest tests/repo/census_rows/test_census_rows_layout.py
"""

import ast
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from functools import cache
from pathlib import PurePosixPath

import pytest
from _helpers import REPO
from _row_table import (
    Census,
    Subject,
    check,
    lines_matching,
    proof_problems,
    tracked,
)
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
    return len(parts) == 5 and parts[3] == "files"


@cache
def _families() -> tuple[Family, ...]:
    """Every group of FAMILY_SIZE or more flat siblings sharing a first or last word."""
    groups: dict[str, list[str]] = defaultdict(list)
    for rel in tracked("scripts/*.py", "k8s/roles/*/files/*.py"):
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
    runs. Its whole body is a docstring, imports and a `__main__` guard holding
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
    # A shim counts out only when it forwards to a real entrypoint in its own family.
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


# ── scripts/ writes nothing to sys.path and imports nothing bare ─────────────────────

_SYS_PATH = {"sys.path", "_sys.path"}
_REACHES_TESTS = "reaches the repo's tests/, which is outside the scripts/ import root"
_SHARED_LONGHORN = (
    "k3s-host-checks copies it beside host_lib.py, where no lib/host_modules ships "
    "(docs/adr/0039-scripts-is-installed-in-place.md)"
)
SYS_PATH_WRITERS = {
    "scripts/lib/host_modules.py": "the one module that adds hosts/lib, gh-quota and the "
    "role modules for its callers",
    "scripts/lib/_claude_worktree.py": "reaches the dotfiles checkout, outside the repo",
    "scripts/dev/affected_lib/traced_pytest.py": "sets sys.path[0] as pytest itself does, "
    "for the run it traces",
    "scripts/dev/pytest_shard.py": _REACHES_TESTS,
    "scripts/dev/shard_prebuild.py": _REACHES_TESTS,
    "scripts/dev/tighten_ratchets.py": _REACHES_TESTS,
    **{
        f"scripts/lib/longhorn_lib/{name}.py": _SHARED_LONGHORN
        for name in (
            "longhorn_backup_health_logic",
            "longhorn_backups",
            "longhorn_cron_evidence_logic",
            "longhorn_etcd_s3_logic",
            "longhorn_skipped_volumes_logic",
        )
    },
}
_LIST_WRITES = {"insert", "append", "extend", "remove", "pop", "clear"}


def _writes_sys_path(node: ast.AST) -> bool:
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        return (
            node.func.attr in _LIST_WRITES and ast.unparse(node.func.value) in _SYS_PATH
        )
    if isinstance(node, ast.Assign | ast.AugAssign):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        return any(
            ast.unparse(t.value if isinstance(t, ast.Subscript) else t) in _SYS_PATH
            for t in targets
        )
    return False


def _sys_path_offence(subject: Subject) -> list[str]:
    return [
        f"line {node.lineno}: {ast.unparse(node)}"
        for node in ast.walk(ast.parse(subject.text))
        if isinstance(node, ast.expr | ast.stmt) and _writes_sys_path(node)
    ]


def _dotted_modules() -> list[str]:
    """scripts/ modules below an area, but not tests or job_files, which ship flat."""
    return [
        rel
        for rel in tracked("scripts/*/*.py")
        if "/tests/" not in rel and not rel.startswith("scripts/renderer/job_files/")
    ]


def _bare_sibling_offence(subject: Subject) -> list[str]:
    """Each import whose top name is a module or package in the importer's own directory."""
    here = REPO / subject.rel.rsplit("/", 1)[0]
    own = subject.rel.rsplit("/", 1)[-1].removesuffix(".py")
    hits = []
    for node in ast.walk(ast.parse(subject.text)):
        if isinstance(node, ast.Import):
            tops = [alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            tops = [node.module.split(".")[0]]
        else:
            continue
        for top in tops:
            if top != own and ((here / f"{top}.py").is_file() or (here / top).is_dir()):
                hits.append(f"line {node.lineno}: {ast.unparse(node)}")
    return hits


# ── Test modules grouped by subject ──────────────────────────────────────────────────

SUITE_CAP = 40
_TO_GROUP = "the Test suite organization milestone groups it"
UNGROUPED_SUITES = dict.fromkeys(
    (
        "hosts/lib/tests",
        "hosts/tests",
        "k8s/roles/monitor-bridge/tests",
        "scripts/validate/tests",
        "tests/services",
    ),
    _TO_GROUP,
)


class Suite(Subject):
    """One directory that holds test modules, with the test directories nested under it."""

    def __init__(self, rel: str, modules: int, groups: Iterable[str] = ()):
        super().__init__(rel, text="")
        self.modules = modules
        self.groups = tuple(groups)


@cache
def _suites() -> tuple[Suite, ...]:
    """Every directory holding a test module directly, as one `Suite` each."""
    counts = Counter(
        str(PurePosixPath(rel).parent) for rel in tracked("test_*.py", "*/test_*.py")
    )
    return tuple(
        Suite(rel, n, [d for d in sorted(counts) if d.startswith(f"{rel}/")])
        for rel, n in sorted(counts.items())
    )


def _suite_offence(subject: Subject) -> list[str]:
    assert isinstance(subject, Suite), subject.rel
    if subject.groups:
        names = ", ".join(PurePosixPath(g).name for g in subject.groups)
        return [f"{subject.modules} test modules beside the groups {names}"]
    if subject.modules > SUITE_CAP:
        return [f"{subject.modules} test modules in one directory, cap {SUITE_CAP}"]
    return []


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
            # Each of the last two fixtures breaks one rule only.
            _fixture("scripts/x/foo_*.py", foo=_MAIN, foo_a=_IMPORTED_LOGIC, foo_b=""),
            _fixture("scripts/x/foo_*.py", foo=_MAIN, foo_a=_TYPE_CHECKING, foo_b=""),
            _fixture("scripts/x/foo_*.py", foo=_MAIN, foo_a=_ARG_CALLS, foo_b=""),
        ),
        green=(
            _fixture("scripts/x/foo_*.py", foo_a="X = 1\n", foo_b="", foo_c=""),
            _fixture("scripts/x/foo_*.py", foo=_MAIN, foo_a=_SHIM, foo_b=_SHIM),
        ),
        # Four groups; the deployer's `gitops_*` family is not one.
        min_matches=4,
    ),
    Census(
        name="no-sys-path-change-under-scripts",
        reason=(
            "`scripts/` is the installed import root, which the venv's `.pth` and each "
            "no-venv launcher supply (docs/adr/0039-scripts-is-installed-in-place.md). A "
            "module that inserts a directory again brings back a second spelling of its "
            "modules, and a test that patches one copy misses the other. Import "
            "`lib.host_modules` for a host or role module; a child process gets its path from "
            "`PYTHONPATH`. `allow` holds the inserts that reach outside the root."
        ),
        files=lambda: tracked("scripts/*.py"),
        offence=_sys_path_offence,
        allow=SYS_PATH_WRITERS,
        red=(
            Subject("a.py", "import sys\nsys.path.insert(0, 'scripts')\n"),
            Subject("b.py", "import sys as _sys\n_sys.path.append(str(DIR))\n"),
            Subject("c.py", "import sys\nsys.path[:0] = ['scripts']\n"),
        ),
        green=(
            Subject("a.py", "from lib import host_modules\nimport ci_verdict\n"),
            Subject(
                "b.py", '"""Reads `sys.path.insert` calls."""\nroots = list(sys.path)\n'
            ),
        ),
        min_matches=1000,
        must_find=frozenset(
            {
                "scripts/deploy_tools/forge.py",
                "scripts/deploy_tools/runbook_gates_lib/gate_runner.py",
                "scripts/deploy_tools/tests/land_merge/test_land_own_checkout.py",
                "scripts/dev/fanout.py",
                "scripts/lib/host_modules.py",
            }
        ),
    ),
    Census(
        name="scripts-import-their-siblings-dotted",
        reason=(
            "A run by path puts the script's own directory first on `sys.path`, so a bare "
            "`import label_worktree` beside it resolves there and fails when another module "
            "loads it dotted. Both spellings in one process are two module objects. Import "
            "from the root: `from dev import label_worktree`. A test's fixture beside it "
            "(`_land_fakes.py`) is outside the selection, since pytest's prepend of the "
            "test's directory is how it is reached, and `renderer/job_files` ships flat."
        ),
        files=_dotted_modules,
        offence=_bare_sibling_offence,
        red=(
            Subject("scripts/dev/x.py", "import label_worktree\n"),
            Subject("scripts/dev/x.py", "def f():\n    from fanout_lib import place\n"),
        ),
        green=(
            Subject("scripts/dev/x.py", "from dev import label_worktree\n"),
            Subject(
                "scripts/lib/longhorn_lib/x.py", "from . import longhorn_backups\n"
            ),
            Subject("scripts/dev/x.py", "import json\nfrom lib import git\n"),
        ),
        min_matches=450,
        must_find=frozenset({"scripts/dev/fanout.py", "scripts/lib/host_modules.py"}),
    ),
    Census(
        name="test-modules-are-grouped-by-subject",
        reason=(
            f"A directory holding more than {SUITE_CAP} test modules is grouped into subject "
            "directories, and a grouped suite holds no test module beside its groups "
            "(.claude/rules/python-layout.md). Move the new module into the group for its "
            "subject, or split the directory. Shared `_*.py` helpers and `conftest.py` stay "
            "at the suite's root."
        ),
        files=_suites,
        offence=_suite_offence,
        allow=UNGROUPED_SUITES,
        red=(
            Suite("scripts/x/tests", SUITE_CAP + 1),
            Suite("scripts/x/tests", 1, ["scripts/x/tests/land"]),
        ),
        green=(
            Suite("scripts/x/tests", SUITE_CAP),
            Suite("scripts/x/tests/land", SUITE_CAP),
        ),
        min_matches=60,
        must_find=frozenset({"scripts/dev/tests/fanout", "tests/deploy"}),
    ),
    Census(
        name="review-runner-is-defined-once",
        reason=(
            "`Runner` is the contract every fake runner in `scripts/dev/tests/_review_fakes.py` "
            "meets. Four copies meant a change to the process boundary's shape landed in four "
            "places. Import it from `dev.fanout_lib.review.processes`, beside `run_process`."
        ),
        files=lambda: [
            rel
            for rel in tracked("scripts/dev/fanout_lib/review/*.py")
            if rel != "scripts/dev/fanout_lib/review/processes.py"
        ],
        offence=lines_matching(re.compile(r"^Runner\s*=")),
        red=(
            Subject(
                "a.py",
                "Runner = Callable[[list[str], str | None], subprocess.CompletedProcess]\n",
            ),
        ),
        green=(
            Subject("a.py", "from dev.fanout_lib.review.processes import Runner\n"),
            Subject("b.py", "def hardened(run: Runner) -> list[str]:\n"),
        ),
        min_matches=10,
        must_find=frozenset(
            {
                "scripts/dev/fanout_lib/review/batch_run.py",
                "scripts/dev/fanout_lib/review/hardened_runs.py",
                "scripts/dev/fanout_lib/review/review_ci.py",
                "scripts/dev/fanout_lib/review/review_land.py",
            }
        ),
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
