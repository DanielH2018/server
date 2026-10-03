"""The AST and text detectors behind the git and subprocess rules for test modules.

`test_census_rows_test_modules.py` runs each as a `Census` row over `pytest_only_modules()`,
and `test_census_rows_test_renders.py` runs the render-and-path detectors over the same census.
This module is the pure half: a function from source text to the offending spellings it found,
so a row's red and green subjects can be two-line snippets. The render-and-path detectors are
the same shape, in `_render_helper_rules.py`.

The rules, in short. A test builds its scratch git repository through `lib.git_testing`, never
by hand: a module that forgets one line of the `GIT_*` scrub writes the primary repository under
`prek`'s pytest hook, where `git commit` exports `GIT_DIR` and `GIT_INDEX_FILE`. A test writes
its fake binaries and its `PATH` through `lib.proc_testing`, which decides the launch flags, the
`PATH` prefix and the exec bit once (#3056). And no test launches a subprocess without a
deadline, because a wedged child parks the whole CI run instead of failing its own test.
"""

import ast
import re
from pathlib import Path

from _row_table import tracked
from test_script_bootstraps_present import is_pytest_only

# Every `testpaths` entry that holds Python, plus `.claude/tests`.
_TEST_ROOTS = (
    "scripts/",
    "ansible/tests/",
    "ansible/roles/",
    ".claude/hooks/",
    ".claude/tests/",
    "evals/",
)


def pytest_only_modules() -> list[str]:
    """Every tracked module `is_pytest_only` accepts under a test root, the rows' one census.

    `ansible/collections/` is vendored third-party code, never judged.
    """
    return [
        rel
        for rel in tracked("*.py")
        if rel.startswith(_TEST_ROOTS)
        and "collections" not in Path(rel).parts
        and is_pytest_only(Path(rel))
    ]


# 950 modules when the census became rows (2026-10-03).
MODULE_FLOOR = 800

# The comprehension a module writes when it re-derives the scrub by filtering on a GIT_ prefix.
GIT_SCRUB = re.compile(r'startswith\(\s*["\']GIT_["\']\s*\)')

# The verbs that build or edit a repository. A read verb against the real checkout -- the
# `git ls-files` twenty-two guards run -- is harmless however the environment is set.
WRITE_VERBS = frozenset(
    {
        "add",
        "am",
        "apply",
        "branch",
        "checkout",
        "cherry-pick",
        "clone",
        "commit",
        "config",
        "fetch",
        "gc",
        "init",
        "merge",
        "mv",
        "prune",
        "pull",
        "push",
        "rebase",
        "remote",
        "reset",
        "restore",
        "rm",
        "stash",
        "switch",
        "tag",
        "update-ref",
        "worktree",
    }
)


def raw_git_calls(source: str) -> list[str]:
    """The WRITE verbs `source` runs through `subprocess`, in either argv spelling.

    A list or a tuple opening with the literal `"git"`. Read from the AST, so a docstring
    naming the old form is prose rather than a hit.
    """
    found = []
    for node in ast.walk(ast.parse(source)):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"run", "Popen", "check_output", "check_call"}
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "subprocess"
            and node.args
        ):
            continue
        argv = node.args[0]
        if not (isinstance(argv, ast.List | ast.Tuple) and len(argv.elts) >= 2):
            continue
        head, verb = argv.elts[0], argv.elts[1]
        if not (isinstance(head, ast.Constant) and head.value == "git"):
            continue
        if not isinstance(verb, ast.Constant) or verb.value not in WRITE_VERBS:
            continue
        # `worktree list` is a read; `worktree add` and `worktree remove` are not.
        third = argv.elts[2] if len(argv.elts) > 2 else None
        if (
            verb.value == "worktree"
            and isinstance(third, ast.Constant)
            and third.value == "list"
        ):
            continue
        found.append(str(verb.value))
    return found


# The launches that take a `timeout=`. `Popen` is deliberately absent: it takes no `timeout=`,
# and its deadline lives on the `wait`/`communicate` that follows.
_BOUNDABLE_LAUNCHES = frozenset({"run", "check_output", "check_call", "call"})

# DECIDED: no fourth rule for Popen. #3073 proposed one — a `Popen` whose enclosing function
# reaches `wait`/`communicate` with no `timeout=` and no preceding `kill()`. The 2026-10-01
# census found seven `Popen` call sites in test modules, and that shape sorts them badly.
#
# It MISSES both sites that are genuinely unboundable. `scripts/diagnostics/tests/
# test_ui_smoke.py`'s `McpClient.__init__` launches a long-lived stdio child whose reads are a
# `readline` loop in another method, and `test_ui_smoke_helpers.py`'s
# `test_close_closes_every_pipe_and_reaps_the_process` hands its child to `close()`. Neither
# function contains a `wait`/`communicate` at all, so a function-scoped AST rule cannot see
# either one — and following the call would mean whole-program analysis.
#
# It HITS almost nothing else. Two of the three sites in `ansible/tests/repo/
# test_process_waits.py` would need an exemption, because that module's subject IS an
# unbounded wait. The third, and `scripts/deploy_tools/tests/test_land_detach.py`, already
# clear on the preceding `kill()`. That leaves a rule whose hit set is mostly its own
# exemption list.
#
# And the spelling it would demand is not safe by itself. `communicate(timeout=...)` raises
# `TimeoutExpired` and leaves the child RUNNING, so a rule satisfied by adding the keyword
# would bless a site that still leaks a process into `filterwarnings = ["error"]`. The one
# real hazard the census found — `scripts/validate/tests/test_vale_sync_guard.py`'s
# `run_concurrently` — needed the deadline AND a `finally` that reaps the batch, and only the
# second half is what actually keeps a wedged guard from parking CI.
#
# What holds the line instead: `lib.proc_testing.run` is the default launch, so reaching for
# `Popen` in a test is already the deliberate act. Contradict this with a measured `Popen`
# site that rule 4 would have caught and `kill()` would not have cleared.

# The module names a `subprocess` import can be bound to. A hit needs the call to reach the
# stdlib module rather than any object that happens to own a `run`, and `sp` is the one alias
# this tree uses.
_SUBPROCESS_NAMES = frozenset({"subprocess", "sp"})

_EXEC_BITS = 0o111


def _join_base(node: ast.expr) -> str | None:
    """The leftmost operand of a `dir / "name"` path join, or None if `node` is not one.

    `(bin_dir / stub).chmod(0o755)` in a loop names a different expression from the
    `(bin_dir / "flock").write_text(...)` that wrote it, so the base is what pairs them.
    """
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _join_base(node.left) or ast.unparse(node.left)
    return None


def handwritten_executables(source: str) -> list[str]:
    """The paths `source` writes and then chmods executable, by the expression it names them by.

    A `chmod` alone is a permission fixture — `0o500` on a state directory, to prove an
    unreadable state reads as a fault rather than as a pass — and is not a hit. The pair is
    what makes it a fake binary, so the chmodded path must also be written in the same
    function, either as the same expression or as a join under the same directory.
    """
    found = []
    for scope in ast.walk(ast.parse(source)):
        if not isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef | ast.Module):
            continue
        writes = [
            node.func.value
            for node in ast.walk(scope)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"write_text", "write_bytes"}
        ]
        written = {ast.unparse(node) for node in writes}
        written_dirs = {base for node in writes if (base := _join_base(node))}
        for node in ast.walk(scope):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "chmod"
                and len(node.args) == 1
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, int)
                and node.args[0].value & _EXEC_BITS
            ):
                continue
            target = node.func.value
            base = _join_base(target)
            if ast.unparse(target) in written or (base and base in written_dirs):
                found.append(ast.unparse(target))
    return sorted(set(found))


def handbuilt_path_prefixes(source: str) -> list[str]:
    """The `PATH` prefixes `source` builds itself, by the expression each is built from.

    A hit is a string that puts something in FRONT of an existing `PATH` value. The two
    spellings in the tree are an f-string (`f"{stub}{os.pathsep}{os.environ['PATH']}"`) and a
    concatenation; both read as a JoinedStr or a BinOp whose text mentions a `PATH` read.
    A literal `PATH` value that names no existing one — a fixture pinning `/usr/bin:/bin` — is
    the subject's own environment rather than a prefix, and is not a hit.
    """
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.JoinedStr | ast.BinOp):
            continue
        text = ast.unparse(node)
        if "PATH" not in text:
            continue
        # A read of an existing PATH is what makes it a prefix rather than a fresh value.
        if not any(
            read in text
            for read in ("os.environ['PATH']", '"PATH"]', "['PATH']", ".get('PATH'")
        ):
            continue
        found.append(text)
    return sorted(set(found))


def unbounded_launches(source: str) -> list[str]:
    """The subprocess launches `source` makes with no deadline, by the text of each.

    A hit is one of two shapes. The first is a call to `subprocess.run` (or `check_output`,
    `check_call`, `call`) carrying no `timeout=` keyword: the child can wedge, and nothing
    above it ever gives up, so the symptom is a CI run that never ends rather than a failing
    test. A `**kwargs` splat counts as bounded, because the keyword can be in it and the AST
    cannot say.

    The second is `from subprocess import run`, which would bind the unbounded launch to the
    bare `run` name that `lib.proc_testing.run` is imported under. A test reading
    `run(["bash", script])` would then have no way to tell which one it got.

    `subprocess.Popen` is not a hit: it accepts no `timeout=`, so there is no bounded spelling
    of it to demand here.
    """
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            found += [
                f"from subprocess import {alias.name}"
                for alias in node.names
                if alias.name in _BOUNDABLE_LAUNCHES
            ]
            continue
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr in _BOUNDABLE_LAUNCHES):
            continue
        if ast.unparse(func.value).split(".")[-1] not in _SUBPROCESS_NAMES:
            continue
        # `**kwargs` can carry the timeout, and `arg is None` is how the AST spells it.
        if any(kw.arg in ("timeout", None) for kw in node.keywords):
            continue
        found.append(ast.unparse(node)[:120])
    return sorted(set(found))
