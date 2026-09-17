"""No test calls an argparse `main()` with no argv, because that argv is PYTEST'S.

With no argument, `parser.parse_args()` falls back to `sys.argv[1:]`. Inside a test that is
pytest's own command line, so any flag the entry point does not define exits its parser with
status 2 before the check under test runs. The defect is invisible under the usual invocation:
three tests in `scripts/diagnostics/tests/test_postflight.py` called `postflight.main()` bare,
passed under `uv run pytest` (xdist workers carry an argv postflight happened to accept), and
failed only under the `-n0 -vv --durations=0` run that `pytest_shard.py --record` uses. That
made `--record` impossible for the whole repo (#1799, #1801; fixed by PR #1797). This guard
holds the CLASS.

The predicate is the hazard, not the call shape. Measured before writing: 35 zero-arg `main()`
calls in the tree, and nearly all are harmless — hook `main()`s read stdin, `renovate_notify`
does a membership test on `sys.argv` that cannot exit, `shell_templates.main(which=...)` takes
an injected callable. Flagging every bare call would demand churn at ~32 sites that cannot
break. So a call is flagged only when the resolved callee's `main` routes argv into
`parse_args` — `parse_args()` bare, `parse_args(argv)` for a parameter of `main`, or
`parse_args(sys.argv[...])` — and the test module neither passes argv nor patches `sys.argv`.
Both remedies the tree already uses are accepted: `main([])` (postflight) and
`monkeypatch.setattr(sys, "argv", [...])` (`fact_cache_guard`, `backfill_staging_gate`).

Run: uv run pytest ansible/tests/repo/test_no_test_reads_pytests_argv.py
"""

import ast
import subprocess
from pathlib import Path, PurePosixPath

from _helpers import REPO, is_test_file

# Non-vacuity floors. Both censuses find their subjects by pattern, so each must be shown to
# contain something concrete — a renamed module or a moved directory would otherwise empty a
# census and let the guard pass over nothing.
KNOWN_ARGV_READERS = frozenset(
    {"postflight", "fact_cache_guard", "backfill_staging_gate"}
)
KNOWN_PATCHED_CALLERS = frozenset(
    {
        "scripts/deploy_tools/tests/test_fact_cache_guard.py",
        "scripts/deploy_tools/tests/test_backfill_staging_gate.py",
    }
)


def _tracked_python() -> list[str]:
    # `git ls-files`, not `rglob`, for the reason `_helpers.discover_docs` records: a
    # root-anchored walk descends into `.claude/worktrees/<name>/` and judges this commit
    # against other sessions' checkouts.
    listed = subprocess.run(
        ["git", "ls-files", "-z", "*.py"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return sorted(rel for rel in listed.split("\0") if rel)


def _mentions_sys_argv(node: ast.AST) -> bool:
    return any(
        isinstance(n, ast.Attribute)
        and n.attr == "argv"
        and isinstance(n.value, ast.Name)
        and n.value.id == "sys"
        for n in ast.walk(node)
    )


def main_reads_argv(source: str) -> bool:
    """Whether `source` defines a `main` whose `parse_args` call reads `sys.argv`.

    `parse_args()` with no argument reads it directly; `parse_args(argv)` reads it when `argv`
    is a parameter of `main` (argparse falls back to `sys.argv` for None); `parse_args(sys.argv
    [1:])` reads it by name. A literal list — `parse_args(["--x"])` — does not.
    """
    tree = ast.parse(source)
    for fn in ast.walk(tree):
        if (
            not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef)
            or fn.name != "main"
        ):
            continue
        params = {
            a.arg for a in fn.args.args + fn.args.posonlyargs + fn.args.kwonlyargs
        }
        for call in ast.walk(fn):
            if not (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "parse_args"
            ):
                continue
            if not call.args and not call.keywords:
                return True
            for arg in [*call.args, *(k.value for k in call.keywords)]:
                if (
                    isinstance(arg, ast.Name) and arg.id in params
                ) or _mentions_sys_argv(arg):
                    return True
    return False


def _patches_sys_argv(tree: ast.AST) -> bool:
    """Whether the test module rebinds `sys.argv` anywhere — body or fixture.

    Accepts `monkeypatch.setattr(sys, "argv", …)`, `setattr(mod.sys, "argv", …)`,
    `setattr("sys.argv", …)`, `mock.patch("sys.argv", …)`, `patch.object(sys, "argv", …)`
    and a direct `sys.argv = …`. Judged per module rather than per test function so a
    patch applied in a fixture counts.
    """
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign):
            if any(
                isinstance(t, ast.Attribute)
                and t.attr == "argv"
                and isinstance(t.value, ast.Name)
                for t in n.targets
            ):
                return True
        elif isinstance(n, ast.Call):
            consts = [a.value for a in n.args if isinstance(a, ast.Constant)]
            if "sys.argv" in consts:
                return True
            if "argv" in consts and any(
                (isinstance(a, ast.Name) and a.id == "sys")
                or (isinstance(a, ast.Attribute) and a.attr == "sys")
                for a in n.args
            ):
                return True
    return False


def _module_bindings(tree: ast.AST) -> dict[str, str]:
    """Local name -> module basename, for `X.main()` and bare `main()` resolution.

    `import a.b.c as x` binds `x` to `c`; `import m` binds `m`; `from p import m` binds `m`;
    `from m import main [as f]` binds `main` (or `f`) to `m`. A name bound any other way —
    `importlib` in the hook tests, a fixture — resolves to nothing and is not judged.
    """
    bound: dict[str, str] = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                bound[a.asname or a.name.split(".")[0]] = a.name.split(".")[-1]
        elif isinstance(n, ast.ImportFrom) and n.module:
            for a in n.names:
                if a.name == "main":
                    bound[a.asname or "main"] = n.module.split(".")[-1]
                else:
                    bound[a.asname or a.name] = a.name
    return bound


def bare_main_calls(source: str) -> list[tuple[int, str]]:
    """`(lineno, module basename)` for every zero-arg `main()` call `source` makes.

    A call the module cannot resolve to an imported module is omitted. A `main()` call with
    ANY argument — positional or keyword — is not bare and is omitted.
    """
    tree = ast.parse(source)
    bound = _module_bindings(tree)
    found = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call) or n.args or n.keywords:
            continue
        f = n.func
        if (
            isinstance(f, ast.Attribute)
            and f.attr == "main"
            and isinstance(f.value, ast.Name)
        ):
            module = bound.get(f.value.id)
        elif isinstance(f, ast.Name) and f.id in bound and f.id == "main":
            module = bound[f.id]
        elif isinstance(f, ast.Name) and bound.get(f.id) and f.id != "main":
            continue
        else:
            continue
        if module:
            found.append((n.lineno, module))
    return found


def offenders(test_source: str, argv_readers: set[str]) -> list[tuple[int, str]]:
    """The bare `main()` calls in `test_source` whose callee reads pytest's argv, unpatched."""
    tree = ast.parse(test_source)
    if _patches_sys_argv(tree):
        return []
    return [
        (ln, mod) for ln, mod in bare_main_calls(test_source) if mod in argv_readers
    ]


def _argv_readers(files: list[str]) -> set[str]:
    readers = set()
    for rel in files:
        if is_test_file(Path(rel)):
            continue
        source = (REPO / rel).read_text(encoding="utf-8")
        if "parse_args" in source and main_reads_argv(source):
            readers.add(PurePosixPath(rel).stem)
    return readers


def test_no_test_calls_an_argparse_main_with_pytests_argv():
    files = _tracked_python()
    readers = _argv_readers(files)
    missing = KNOWN_ARGV_READERS - readers
    assert not missing, (
        f"argv-reader census lost {sorted(missing)}; it may now be vacuous"
    )

    failures = []
    patched_callers = set()
    for rel in files:
        if not Path(rel).name.startswith("test_"):
            continue
        source = (REPO / rel).read_text(encoding="utf-8")
        if not source.count("main()"):
            continue
        calls = [c for c in bare_main_calls(source) if c[1] in readers]
        if calls and _patches_sys_argv(ast.parse(source)):
            patched_callers.add(rel)
        failures += [
            f"{rel}:{ln} calls {mod}.main() with no argv"
            for ln, mod in offenders(source, readers)
        ]

    lost = KNOWN_PATCHED_CALLERS - patched_callers
    assert not lost, f"caller census lost {sorted(lost)}; it may now be vacuous"
    assert not failures, (
        "A bare main() over an argparse entry point reads PYTEST's argv and exits 2 under any "
        "flag it does not define (invisible under `-n auto`, fatal under `pytest_shard.py "
        "--record`). Pass `main([])` / an explicit argv, or patch `sys.argv`:\n"
        + "\n".join(failures)
    )


# ---- red proof: the predicate is exercised on fixture sources, one accepting, one rejecting

ARGPARSE_MAIN = """
import argparse
def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--x")
    return ap.parse_args(argv)
"""

BARE_PARSE_ARGS_MAIN = """
import argparse
def main():
    return argparse.ArgumentParser().parse_args()
"""

LITERAL_ARGV_MAIN = """
import argparse
def main():
    return argparse.ArgumentParser().parse_args(["--x"])
"""

NO_PARSER_MAIN = """
import sys, json
def main():
    return json.load(sys.stdin)
"""


def test_main_routing_argv_into_parse_args_is_a_reader():
    assert main_reads_argv(ARGPARSE_MAIN)
    assert main_reads_argv(BARE_PARSE_ARGS_MAIN)


def test_main_with_a_literal_or_no_parser_is_not_a_reader():
    assert not main_reads_argv(LITERAL_ARGV_MAIN)
    assert not main_reads_argv(NO_PARSER_MAIN)


def test_bare_main_over_an_argparse_entry_point_is_flagged():
    test = "import postflight\ndef test_x():\n    postflight.main()\n"
    assert offenders(test, {"postflight"}) == [(3, "postflight")]
    aliased = "import scripts.diagnostics.postflight as pf\ndef test_x():\n    assert pf.main() == 0\n"
    assert offenders(aliased, {"postflight"}) == [(3, "postflight")]
    imported = "from postflight import main\ndef test_x():\n    main()\n"
    assert offenders(imported, {"postflight"}) == [(3, "postflight")]


def test_main_with_explicit_argv_is_clean():
    test = "import postflight\ndef test_x():\n    postflight.main([])\n"
    assert offenders(test, {"postflight"}) == []


def test_bare_main_with_sys_argv_patched_is_clean():
    for patch in (
        'monkeypatch.setattr(sys, "argv", ["x"])',
        'monkeypatch.setattr(g.sys, "argv", ["x"])',
        'monkeypatch.setattr("sys.argv", ["x"])',
        'sys.argv = ["x"]',
    ):
        test = f"import sys\nimport g\ndef test_x(monkeypatch):\n    {patch}\n    g.main()\n"
        assert offenders(test, {"g"}) == [], patch


def test_bare_main_over_a_parserless_entry_point_is_clean():
    test = "import hook\ndef test_x():\n    hook.main()\n"
    assert offenders(test, {"postflight"}) == []
