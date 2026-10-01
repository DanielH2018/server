"""Three source scanners over every tracked Python file, sharing one `git ls-files` census.

Each rule below walks the same tree for a different hazard, so the census is listed once and
each file is read once. The rules stay separate tests with separate non-vacuity floors and
separate red-proof pairs: merging the walk must not merge the evidence that each rule can fail.

1. No file walks from repo root with `.rglob(`.
   Recurring-failure class 7 (docs/failure-classes.md): "the execution context is not the
   shell you developed in." A root-anchored `rglob` descends into `.claude/worktrees/<name>/` —
   a full checkout per live session, holding OLDER copies of the same docs — so a guard judges
   this commit against another session's history. `git ls-files` answers with what the commit
   actually contains (see `_helpers.discover_docs`). A tracked `.py` file must not call `.rglob(` on `REPO`, `REPO_ROOT`, `repo_root`
   or `repo`. Scoping an `rglob` under a narrower subdirectory (`ansible/rglob(...)`,
   `ROLES.rglob(...)`) is fine: `.claude/worktrees/` sits at repo root, not under any of those.

2. No test builds a fixture from the live clock.
   A test that stamps its fixture `time.time() - age` and hands it to code that reads
   `time.time()` itself asserts a distance measured across the suite's own runtime. The
   distance is usually fine and occasionally straddles a threshold — an exact day count
   crossing midnight, a "fresh" marker read after a slow collection, a `1.0 days ago` message
   that rounds to `1.1`. Every function these tests exercise takes a `now` (or a `clock`), so
   the deterministic form is a fixed epoch per module handed to both sides. A
   subprocess-driven test is no exception: the Longhorn reader and reaper entry points take
   `main(..., now=None)`, and their harnesses run them through a one-line `runpy` shim that
   calls `main` with a fixed epoch — still a subprocess, still the real env parsing, but no
   seam the cron's environment can reach. `SUBPROCESS_DRIVEN` is the list of files
   still excused on that ground; it is empty, and the guard fails if a listed file stops
   reading the clock, so it can only shrink.

3. No test calls an argparse `main()` with no argv, because that argv is PYTEST'S.
   With no argument, `parser.parse_args()` falls back to `sys.argv[1:]`. Inside a test that is
   pytest's own command line, so any flag the entry point does not define exits its parser with
   status 2 before the check under test runs. Under `uv run pytest`, xdist workers can carry an
   argv the entry point happens to accept, so the failure shows only under the
   `-n0 -vv --durations=0` run that `pytest_shard.py --record` uses, which would make `--record`
   impossible for the whole repo.
   The predicate is the hazard, not the call shape. Most zero-arg
   `main()` calls in the tree are harmless — hook `main()`s read stdin,
   `renovate_notify` does a membership test on `sys.argv` that cannot exit,
   `shell_templates.main(which=...)` takes an injected callable. So a call is flagged only when
   the resolved callee's `main` routes argv into `parse_args` — `parse_args()` bare,
   `parse_args(argv)` for a parameter of `main`, or `parse_args(sys.argv[...])` — and the test
   module neither passes argv nor patches `sys.argv`. Both remedies the tree already uses are
   accepted: `main([])` (postflight) and `monkeypatch.setattr(sys, "argv", [...])`
   (`fact_cache_guard`).

Run: uv run pytest ansible/tests/repo/test_tracked_python_hazards.py
"""

import ast
import functools
import re
from pathlib import Path, PurePosixPath

from _helpers import REPO, is_test_file
from lib.proc_testing import run


@functools.cache
def _tracked_python() -> tuple[str, ...]:
    # `git ls-files`, not `rglob`, for the reason rule 1 exists: a root-anchored walk descends
    # into `.claude/worktrees/<name>/` and judges this commit against other sessions' checkouts.
    listed = run(["git", "ls-files", "-z", "--", "*.py"], cwd=REPO, check=True).stdout
    return tuple(sorted(rel for rel in listed.split("\0") if rel))


@functools.cache
def _source(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8", errors="replace")


def test_the_scan_finds_tracked_python_files():
    """Without this, every rule below passes vacuously on an empty file list."""
    assert len(_tracked_python()) >= 100


# ---- rule 1: no root-anchored rglob

# Matches `<name>.rglob(` where <name> is one of the repo-root variable names this repo uses,
# case-sensitive to the conventions seen in `_helpers.py` and the `ansible/tests/` guards.
ROOT_ANCHORED_RGLOB = re.compile(r"\b(REPO|REPO_ROOT|repo_root|repo)\.rglob\(")

# test_adr_links.py rglobs from REPO but filters every result through a `SKIP_PARTS` set that
# names "worktrees" explicitly, with its own comment on the same hazard this guard exists for
# (its `_source_files` docstring). That is a second, independently-argued mitigation for the
# same class-7 incident, not an unguarded instance — named here rather than left to the regex
# to relearn what its own comment already states.
ALREADY_MITIGATED = {"ansible/tests/repo/test_adr_links.py"}

# A scanner must not scan itself: this file's own red-proof fixtures quote the exact
# `REPO.rglob(` shape it is looking for, as strings rather than code.
SELF = "ansible/tests/repo/test_tracked_python_hazards.py"


def test_no_tracked_file_rglobs_from_repo_root():
    assert SELF in _tracked_python(), (
        "SELF no longer names this file; it would flag itself"
    )
    offenders = [
        rel
        for rel in _tracked_python()
        if rel not in ALREADY_MITIGATED
        and rel != SELF
        and ROOT_ANCHORED_RGLOB.search(_source(rel))
    ]
    assert not offenders, (
        f"{offenders} call .rglob() directly on a repo-root path. That walks whatever is on "
        f"disk, including other sessions' `.claude/worktrees/<name>/` checkouts, and judges "
        f"this commit against their older copies of the same files. Use `git ls-files` "
        f"(see _helpers.discover_docs) or scope the rglob under a narrower subdirectory that "
        f"cannot contain `.claude/worktrees/`."
    )


def test_the_pattern_rejects_a_root_anchored_rglob_and_accepts_a_scoped_one():
    """Red-proof pair for ROOT_ANCHORED_RGLOB itself."""
    assert ROOT_ANCHORED_RGLOB.search("for p in REPO.rglob('*.md'): ...")
    assert not ROOT_ANCHORED_RGLOB.search("for p in ROLES.rglob('*.md'): ...")
    assert not ROOT_ANCHORED_RGLOB.search(
        "for p in (REPO / 'ansible').rglob('*.md'): ..."
    )


# ---- rule 2: no live clock in a test

# A subprocess-driven test whose `now` cannot cross the process boundary. Empty;
# an entry here needs a reason the `runpy` shim in `_reap_entrypoint_harness.py` does not
# cover, and is removed again the moment the file sheds its clock read.
SUBPROCESS_DRIVEN: frozenset[str] = frozenset()

# Non-vacuity floor: files the census must reach, so a moved `tests/` directory or a renamed
# module cannot empty it and let the guard pass over nothing. The census is every tracked
# module under a `tests/` directory, not just `test_*.py`: a `_*_fixtures.py` helper that
# stamps `time.time() - age` reproduces the defect behind every test that imports it.
KNOWN_TEST_FILES = frozenset(
    {
        "scripts/diagnostics/tests/_alert_fixtures.py",
        "scripts/dev/tests/test_findings_claim_staleness.py",
        "ansible/roles/k8s/monitor-bridge/tests/test_check_r2.py",
        "ansible/roles/setup/k3s/tests/test_longhorn_backup_health_reader.py",
    }
)

# (attribute called, name of the object it is called on). `datetime.datetime.now` and
# `_dt.datetime.now` both end in `datetime.now`, so the owner is the LAST attribute or the
# bare name — never the import alias.
_CLOCK_CALLS = frozenset(
    {
        ("time", "time"),
        ("now", "datetime"),
        ("utcnow", "datetime"),
        ("today", "date"),
        ("today", "datetime"),
    }
)


def _owner_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def clock_reads(source: str) -> list[int]:
    """Line numbers of every `time.time()`, `datetime.now()`, `utcnow()` or `today()` call."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        owner = _owner_name(node.func.value)
        if owner and (node.func.attr, owner) in _CLOCK_CALLS:
            found.append(node.lineno)
    return sorted(found)


def test_no_test_file_reads_the_live_clock():
    files = [rel for rel in _tracked_python() if is_test_file(Path(rel))]
    missing = KNOWN_TEST_FILES - set(files)
    assert not missing, (
        f"test-file census lost {sorted(missing)}; it may now be vacuous"
    )

    failures = []
    still_listed = set()
    for rel in files:
        lines = clock_reads(_source(rel))
        if not lines:
            continue
        if rel in SUBPROCESS_DRIVEN:
            still_listed.add(rel)
            continue
        failures += [f"{rel}:{ln}" for ln in lines]

    shed = SUBPROCESS_DRIVEN - still_listed
    assert not shed, (
        f"{sorted(shed)} no longer read the clock; drop them from SUBPROCESS_DRIVEN"
    )
    assert not failures, (
        "A test fixture dated against the live clock asserts a distance measured across the "
        "suite's own runtime. Pin a module-level epoch and pass it as `now=` (or `clock=`) "
        "to the code under test — every callee here takes one:\n" + "\n".join(failures)
    )


LIVE_CLOCK = """
import time
import datetime as _dt
from datetime import datetime
stamp = time.time() - 60
a = datetime.now()
b = _dt.datetime.now(tz=_dt.UTC)
c = _dt.date.today()
"""

FIXED_EPOCH = """
from datetime import datetime, UTC
NOW = 1_780_000_000.0
stamp = NOW - 60
when = datetime(2026, 9, 21, tzinfo=UTC)
age = time_of(NOW)  # a call named `time` on something other than the module is not a read
"""


def test_a_live_clock_read_is_flagged():
    assert clock_reads(LIVE_CLOCK) == [5, 6, 7, 8]


def test_a_fixed_epoch_is_clean():
    assert clock_reads(FIXED_EPOCH) == []


# ---- rule 3: no bare main() over an argparse entry point

# Non-vacuity floors. Both censuses find their subjects by pattern, so each must be shown to
# contain something concrete — a renamed module or a moved directory would otherwise empty a
# census and let the guard pass over nothing.
KNOWN_ARGV_READERS = frozenset({"postflight", "fact_cache_guard"})
KNOWN_PATCHED_CALLERS = frozenset(
    {
        "scripts/deploy_tools/tests/test_fact_cache_guard.py",
    }
)


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


def _argv_readers(files: tuple[str, ...]) -> set[str]:
    readers = set()
    for rel in files:
        if is_test_file(Path(rel)):
            continue
        source = _source(rel)
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
        source = _source(rel)
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
