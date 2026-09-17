"""A repair command that runs the suite before it writes must deselect the guard it repairs.

The shape (#1799, #1877): a self-healing command refuses to write unless "everything passes",
and one of the things that must pass is the assertion the command exists to satisfy. The
guard is red exactly when the repair is needed, so the repair can never run. Measured
2026-09-11: 126 of 629 test files unweighted against a 20% cap, the coverage ratchet red, and
two consecutive `pytest_shard.py --record` runs wrote nothing. PR #1797 broke that instance by
deselecting the ratchet during the record run. This module is the sweep #1877 asked for: the
census of every regenerate-then-commit path in the tree, each with a ruling, and a guard over
the one shape that can deadlock.

The census, measured 2026-09-17. A path deadlocks only when its precondition is evaluated
BEFORE the repair writes and the guard reads the pre-repair state. Every other order is safe.

- `scripts/dev/pytest_shard.py --record`. Runs the full serial suite and writes the weights
  only if it passed. `test_the_recorded_weights_still_cover_most_of_the_suite` reads the
  weights the run has not written yet. DEADLOCKED without the `--deselect` below; guarded here.
- `scripts/docs/gen_doc_fragments.py`. Writes unconditionally. Its guard,
  `test_every_committed_fragment_matches_what_the_generator_writes_now`, runs at commit time
  over the regenerated output. Not deadlocked.
- `scripts/secrets_mgmt/secret_rotation.py sync`. Writes the registry unconditionally. Its
  guard, the `secret-rotation-registry-sync` prek hook (`audit --check`), runs at commit time
  over the synced registry. Not deadlocked.
- `scripts/validate/refresh_vendored_schemas.py`. Writes the schemas unconditionally, and
  no guard compares the vendored copy to upstream (that is the point of vendoring). Not
  deadlocked.
- The docs-refresh cron (`docs-refresh.sh.j2`). Regenerates first, then commits with the
  prek chain on, so the `pytest` hook (`always_run`) sees the regenerated output. Not
  deadlocked. A generator that FAILS leaves stale output for the fragment guard to reject,
  which is the guard doing its job; the alert names the hook.
- The eval-run cron (`eval-run.sh.j2`). Writes `evals/history.json`, then commits with the
  prek chain on. No test reads the committed `evals/history.json` (the ones that name it
  build their own under `tmp_path`). Not deadlocked.
- The secret-rotate cron (`secret-rotate.sh.j2`). Commits `--no-verify`, so it has no
  precondition at all; CI validates the push server-side. Not deadlocked.

The adjacent hazard the sweep found, not the self-satisfying shape: the two crons that commit
with hooks on run the ENTIRE suite, so a ratchet that only a manual command can repair (the
weights ratchet is the one instance) blocked their commits while it was red (#1899). Both now
deselect it through PYTEST_ADDOPTS around their commit, the way `--record` deselects it in
its argv; CI's sharded job still enforces it. That deselect is shell, not a subprocess argv,
so the AST guard below cannot see it --
`ansible/tests/setup/test_crons_deselect_the_weights_ratchet.py` pins the templates to
`pytest_shard.RATCHET_NODE_ID` instead.

The guard: every first-party script that runs pytest as a subprocess must pass `--deselect`
in that same argv. Found by AST rather than by text, so a `subprocess.run([...])` spread over
lines still counts and a docstring mentioning pytest does not. `KNOWN_INVOKERS` is the
non-vacuity floor: the census must find `pytest_shard.py`, or the walk is reading nothing.

Run: uv run pytest ansible/tests/repo/test_repair_commands_deselect_their_own_ratchet.py
"""

import ast
from pathlib import Path

from _helpers import REPO

SCRIPT_ROOTS = (REPO / "scripts", REPO / "ansible/roles", REPO / "evals")
KNOWN_INVOKERS = frozenset({"scripts/dev/pytest_shard.py"})
_SUBPROCESS_CALLS = frozenset({"run", "call", "check_call", "check_output", "Popen"})


def _string_constants(node: ast.AST) -> set[str]:
    return {
        n.value
        for n in ast.walk(node)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }


def _is_subprocess_call(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr in _SUBPROCESS_CALLS
    return isinstance(func, ast.Name) and func.id in _SUBPROCESS_CALLS


def suite_runs_without_deselect(source: str) -> list[int]:
    """Line numbers of subprocess calls that run pytest and pass no `--deselect`.

    A call counts as running pytest when its first positional argument carries the string
    `pytest` anywhere in it: `["uv", "run", "pytest"]` and `[sys.executable, "-m", "pytest"]`
    both match. Empty means clean.
    """
    tree = ast.parse(source)
    offenders: list[int] = []
    for node in ast.walk(tree):
        if (
            not isinstance(node, ast.Call)
            or not _is_subprocess_call(node)
            or not node.args
        ):
            continue
        argv = _string_constants(node.args[0])
        if "pytest" in argv and "--deselect" not in argv:
            offenders.append(node.lineno)
    return offenders


def _first_party_scripts() -> list[Path]:
    return sorted(
        path
        for root in SCRIPT_ROOTS
        for path in root.rglob("*.py")
        if "tests" not in path.parts and "collections" not in path.parts
    )


def _runs_pytest(path: Path) -> bool:
    tree = ast.parse(path.read_text())
    return any(
        isinstance(node, ast.Call)
        and _is_subprocess_call(node)
        and node.args
        and "pytest" in _string_constants(node.args[0])
        for node in ast.walk(tree)
    )


def test_the_census_finds_the_invoker_it_must_find():
    found = {
        str(path.relative_to(REPO))
        for path in _first_party_scripts()
        if _runs_pytest(path)
    }
    assert KNOWN_INVOKERS <= found, f"missing: {sorted(KNOWN_INVOKERS - found)}"


def test_every_script_that_runs_the_suite_deselects_a_guard():
    offenders = {
        str(path.relative_to(REPO)): lines
        for path in _first_party_scripts()
        if (lines := suite_runs_without_deselect(path.read_text()))
    }
    assert not offenders, (
        f"{offenders}: a script that runs the suite before it writes cannot repair a guard "
        "that is red because the write has not happened yet. Deselect that guard the way "
        "pytest_shard.record_weights does, or drop the green-suite precondition."
    )


def test_the_deselected_node_id_names_a_real_test():
    # The deselect is only a repair if the name it carries is the ratchet's real name; a
    # renamed ratchet would leave `--deselect` pointing at nothing and the deadlock back.
    source = (REPO / "scripts/dev/pytest_shard.py").read_text()
    constants = _string_constants(ast.parse(source))
    node_ids = [c for c in constants if "::test_" in c]
    # The f-string body is split by ast; the test name is its literal tail.
    names = [c.split("::", 1)[1] for c in node_ids]
    assert names, "pytest_shard.py carries no `<path>::test_...` node id"
    ratchet = (
        REPO / "ansible/tests/repo/test_pytest_shards_partition_the_suite.py"
    ).read_text()
    missing = [name for name in names if f"def {name}(" not in ratchet]
    assert not missing, f"deselected but not defined in the ratchet module: {missing}"


# --- red-proof pair for the pure core --------------------------------------------------


def test_a_suite_run_with_a_deselect_is_clean():
    source = (
        "import subprocess\n"
        'subprocess.run([sys.executable, "-m", "pytest", "--deselect", "x.py::test_y"])\n'
    )
    assert suite_runs_without_deselect(source) == []


def test_a_suite_run_without_a_deselect_is_flagged():
    source = 'import subprocess\nsubprocess.run(["uv", "run", "pytest", "-q"])\n'
    assert suite_runs_without_deselect(source) == [2]


def test_a_docstring_mentioning_pytest_is_not_a_suite_run():
    source = (
        '"""Run: uv run pytest scripts"""\nimport subprocess\nsubprocess.run(["ls"])\n'
    )
    assert suite_runs_without_deselect(source) == []
