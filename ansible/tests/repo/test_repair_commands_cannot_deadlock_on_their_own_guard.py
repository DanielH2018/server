"""A repair command that runs the suite before it writes must not be gated on what it writes.

The shape: a self-healing command refuses to write unless "everything passes", and one of the
things that must pass is the assertion the command exists to satisfy. The guard is red exactly
when the repair is needed, so the repair can never run. No instance exists in the tree. This
module is the census of every regenerate-then-commit path, each with a ruling.

A path deadlocks only when its precondition is evaluated BEFORE the repair writes and the
guard reads the pre-repair state. Every other order is safe.

- `scripts/dev/pytest_shard.py --record`. Runs the full serial suite and writes the weights
  only if it passed. No test in the suite reads the weights table for staleness, so the
  precondition cannot refuse to clear itself. Not deadlocked.
- `scripts/docs/gen_doc_fragments.py`. Writes unconditionally. Its guard,
  `test_every_committed_fragment_matches_what_the_generator_writes_now`, runs at commit time
  over the regenerated output. Not deadlocked.
- `scripts/dev/tighten_ratchets.py --tighten`. Lowers each ratchet allowlist entry to what
  its file is today, unconditionally and without running the suite. Its guard, the length and
  monkeypatch ratchets in `test_module_length_ratchet.py`, runs at commit time over the
  rewritten lists — the `tighten-ratchet-allowlists` prek hook sits above `pytest` so the
  order holds. Not deadlocked, and deliberately so: the ratchet it repairs is red exactly
  when the repair is needed, so a green-suite precondition here would deadlock.
- `scripts/secrets_mgmt/secret_rotation.py sync`. Writes the registry unconditionally. Its
  guard, the `secret-rotation-registry-sync` prek hook (`audit --check`), runs at commit time
  over the synced registry. Not deadlocked.
- `scripts/validate/refresh_vendored_schemas.py`. Writes the schemas unconditionally, and
  no guard compares the vendored copy to upstream (that is the point of vendoring). Not
  deadlocked.
- The docs-refresh cron (`docs-refresh.sh.j2`). Regenerates first, then runs the suite
  explicitly and commits, so the run sees the regenerated output. Not deadlocked. A generator
  that FAILS leaves stale output for the fragment guard to reject, which is the guard doing
  its job; the alert names the gate.
- The eval-run cron (`eval-run.sh.j2`). Writes `evals/history.json`, then runs the suite and
  commits. No test reads the committed `evals/history.json` (the ones that name it build
  their own under `tmp_path`). Not deadlocked.
- The secret-rotate cron (`secret-rotate.sh.j2`). Commits `--no-verify`, so it has no
  precondition at all; CI validates the push server-side. Not deadlocked.

The guard: a first-party script that runs pytest as a subprocess must appear in `RULINGS`
above-by-name, so a NEW repair command cannot land without someone writing down why it cannot
deadlock. Found by AST rather than by text, so a `subprocess.run([...])` spread over lines
still counts and a docstring mentioning pytest does not. The census is its own non-vacuity
floor: `RULINGS` names `pytest_shard.py`, and a walk that read nothing would not find it.

Run: uv run pytest ansible/tests/repo/test_repair_commands_cannot_deadlock_on_their_own_guard.py
"""

import ast
from pathlib import Path

from _helpers import REPO

SCRIPT_ROOTS = (REPO / "scripts", REPO / "ansible/roles", REPO / "evals")
_SUBPROCESS_CALLS = frozenset({"run", "call", "check_call", "check_output", "Popen"})

# Every first-party script that runs the suite as a subprocess, against the reason it cannot
# deadlock. The docstring above carries the full ruling for each.
RULINGS = {
    "scripts/dev/pytest_shard.py": (
        "writes the weights only on a green run, and since #2830 no test reads the weights "
        "table, so the precondition cannot be red because the write has not happened"
    ),
}


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


def suite_run_lines(source: str) -> list[int]:
    """Line numbers of subprocess calls that run pytest. Empty means the script never does.

    A call counts as running pytest when its first positional argument carries the string
    `pytest` anywhere in it: `["uv", "run", "pytest"]` and `[sys.executable, "-m", "pytest"]`
    both match.
    """
    tree = ast.parse(source)
    return sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and _is_subprocess_call(node)
        and node.args
        and "pytest" in _string_constants(node.args[0])
    )


def _first_party_scripts() -> list[Path]:
    return sorted(
        path
        for root in SCRIPT_ROOTS
        for path in root.rglob("*.py")
        if "tests" not in path.parts and "collections" not in path.parts
    )


def _suite_invokers() -> set[str]:
    return {
        str(path.relative_to(REPO))
        for path in _first_party_scripts()
        if suite_run_lines(path.read_text())
    }


def test_the_census_finds_the_invoker_it_must_find():
    """Non-vacuity. A broken walk finds nothing, and the ruling check below passes over it."""
    assert set(RULINGS) <= _suite_invokers(), (
        f"missing: {sorted(set(RULINGS) - _suite_invokers())}"
    )


def test_every_script_that_runs_the_suite_carries_a_ruling():
    unruled = sorted(_suite_invokers() - set(RULINGS))
    assert not unruled, (
        f"{unruled}: a script that runs the suite before it writes can deadlock on a guard "
        "that is red because the write has not happened yet. Rule on it in this module's "
        "docstring and add it to RULINGS, or drop the green-suite precondition."
    )


# --- red-proof pair for the pure core --------------------------------------------------


def test_a_suite_run_is_found():
    source = 'import subprocess\nsubprocess.run(["uv", "run", "pytest", "-q"])\n'
    assert suite_run_lines(source) == [2]


def test_a_docstring_mentioning_pytest_is_not_a_suite_run():
    source = (
        '"""Run: uv run pytest scripts"""\nimport subprocess\nsubprocess.run(["ls"])\n'
    )
    assert suite_run_lines(source) == []
