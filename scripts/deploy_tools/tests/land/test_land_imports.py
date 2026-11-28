"""land_lib's dependency direction: phases import the state, never each other's peers or the pipeline.

Modules under `core/` import only each other and the stdlib modules in `PURE_STDLIB`.

Run: uv run pytest scripts/deploy_tools/tests/land/test_land_imports.py
"""

import ast
from pathlib import Path

LIB = Path(__file__).resolve().parents[2] / "land_lib"
# The pure decisions under `core/`, named as paths from `land_lib/`.
CORE = frozenset(
    {
        "core/queue_state",
        "core/queue_machine",
        "core/invariants",
        "core/landing_state",
        "core/landing_actions",
        "core/landing_machine",
    }
)
# What a module under `core/` may import beside its siblings: none of these does I/O.
PURE_STDLIB = frozenset(
    {"collections.abc", "dataclasses", "enum", "functools", "itertools", "typing"}
)
MODULES = frozenset(
    {
        "outcome",
        "options",
        "tools",
        "ledger",
        "landing",
        "merge",
        "serial_lock",
        "turn",
        "pr_lock",
        "slots",
        "line",
        "batch",
        "stale_base",
        "stall",
        "stuck_checks",
        "failing_checks",
        "policy",
        "classify",
        "closing_refs",
        "ci",
        "deploy",
        "promotion",
        "health_verdict",
        "own_checkout",
        "fresh",
        "pipeline",
        "detach",
        "live_limit",
        "handoff",
        "host_wait",
        "host_published",
        "host_schedule",
        "compose_wait",
        "pr_json",
        "gh_read",
        "land_flux",
        "land_probe",
        "land_tags",
        "health_gate",
        "land_tested_base",
        "pin_gate",
        "pin_reads",
        "verdict_log",
        "transitions",
        *CORE,
    }
)
ALLOWED = {
    "outcome": set(),
    "options": set(),
    # `cause`'s vocabulary lives beside `verdict`'s, and the Ledger validates against it.
    "ledger": {"outcome"},
    # The classifier libraries the Tools defaults wrap.
    "tools": {
        "land_flux",
        "own_checkout",
        # `Tools.fresh_landing`, the checkout `--fresh` lands from.
        "fresh",
        # `Tools.fork`, the detached process `--detach` lands in.
        "detach",
        "land_tags",
        "land_tested_base",
        # `Tools.gate`, the health gate the landing ends with.
        "health_gate",
        # The per-user verdict record `Tools.record_verdict` writes by default.
        "verdict_log",
    },
    # The record `probe.py landing-verdicts` reads; a leaf, written through `Tools`.
    "verdict_log": set(),
    # The PR JSON types are a leaf: `Landing.view` returns them and the policy reads them.
    "pr_json": set(),
    # The retry around a read-only gh call: `Landing.view` uses it, and it prints with `say`.
    "gh_read": {"outcome"},
    "landing": {
        "outcome",
        "options",
        "tools",
        "ledger",
        "pr_json",
        "land_tags",
        "gh_read",
        "transitions",
    },
    # The landing policy's checks run inside --arm-merge, before any merge call.
    "merge": {
        "closing_refs",
        "failing_checks",
        "landing",
        "outcome",
        "pin_gate",
        "policy",
        "pr_json",
        "gh_read",
        "slots",
        "stale_base",
        "stall",
        "stuck_checks",
        "transitions",
        "core/landing_actions",
        "core/landing_state",
    },
    # The merge wait's decision about a head whose CI no longer moves, and the run it starts.
    "stall": {"landing", "outcome", "gh_read", "core/landing_state"},
    # What makes a green head stale against master, and the one branch update.
    "stale_base": {"landing", "outcome", "gh_read", "policy", "pr_json"},
    "turn": {
        "landing",
        "outcome",
        "line",
        "fresh",
        "serial_lock",
        "core/landing_state",
        "core/queue_machine",
        "core/queue_state",
    },
    # One live landing per PR: a lock beside the turn, taken before a landing forks or merges.
    "pr_lock": {"turn", "serial_lock"},
    # The stdlib-only read of the serial lock, which `cc-wait landing-clear` runs without the venv.
    "serial_lock": set(),
    # The lock files that bound the landings polling GitHub after their merge, beside the turn.
    "slots": {"landing", "outcome", "live_limit", "pin_gate", "turn", "serial_lock"},
    # The drafts whose landings wait for the turn, which its holder reads; boundaries only.
    "line": {"serial_lock", "core/queue_state"},
    # The writer of a landing's state record and transition line, light for the deploy UI.
    "transitions": {"line", "outcome", "core/landing_state"},
    # The drafts in the line, tested with the turn holder's own in one run, then merged.
    "batch": {
        "landing",
        "outcome",
        "classify",
        "gh_read",
        "line",
        "live_limit",
        "merge",
        "slots",
        "stale_base",
        "turn",
        "transitions",
        "core/landing_state",
        "core/queue_machine",
        "core/queue_state",
    },
    # The read that ends --await-merge on a check GitHub never finishes.
    "stuck_checks": {"landing", "gh_read"},
    # The read that ends --await-merge on a merge refused while a required check has failed.
    "failing_checks": {"landing", "gh_read"},
    # The pin gate runs inside --await-merge's direct merge, and `land.py --regate` alone.
    "pin_gate": {"landing", "outcome", "pin_reads"},
    # Its boundaries. It imports `promote.py`'s `last-good` reader, outside land_lib.
    "pin_reads": set(),
    "policy": {"landing", "outcome", "pr_json", "gh_read"},
    "classify": {
        "landing",
        "outcome",
        "land_tags",
        "own_checkout",
        "promotion",
        # The stale-code refusals name `--fresh` as their remedy.
        "fresh",
    },
    "closing_refs": {"gh_read", "landing", "outcome"},
    "ci": {"landing", "outcome", "transitions"},
    "deploy": {
        "landing",
        "outcome",
        "promotion",
    },
    "promotion": {"landing", "outcome", "transitions"},
    "health_verdict": {"landing", "outcome", "promotion", "health_gate"},
    "own_checkout": set(),
    # Makes a checkout and runs a child land.sh; it imports no phase. `grading_argv` reads Options.
    "fresh": {"options"},
    # `fresh.dies_on_a_signal`, so a stopped landing unwinds and records its code.
    "detach": {"fresh"},
    # A PR's kind and the quota floor: GitHub reads only, nothing that drives a landing.
    "live_limit": set(),
    "handoff": {"options", "outcome"},
    # The tag derivation and what it reads. None of them knows what a landing is.
    "land_tags": set(),
    # `probe.py health` per tag; it imports no phase.
    "health_gate": set(),
    # H3.9a: the wait on the hosts a merge reaches. It takes `gate_snapshot` from `tools`.
    "host_wait": {
        "landing",
        "outcome",
        "tools",
        "compose_wait",
        "host_published",
        "host_schedule",
        "transitions",
    },
    "land_flux": {"pin_reads"},
    "land_tested_base": set(),
    # cc-wait's `land` source runs under `uv run --no-project`, so it reads only the stdlib-only
    # fork record and the verdict words.
    "land_probe": {"detach", "outcome"},
    "compose_wait": {"outcome"},
    "host_published": {"outcome"},
    "host_schedule": set(),
    "pipeline": {
        "landing",
        "outcome",
        "batch",
        "merge",
        "turn",
        "slots",
        "classify",
        "closing_refs",
        "ci",
        "deploy",
        "health_verdict",
        "host_wait",
        "core/landing_state",
    },
    # The turn's ordering decisions: pure, so the queue's state machine drives them as is.
    "core/queue_state": set(),
    "core/queue_machine": {"core/queue_state"},
    "core/invariants": {"core/queue_state"},
    # The merge wait's state and its transition function, which the loop's traces hold.
    "core/landing_state": set(),
    "core/landing_actions": {"core/landing_state"},
    "core/landing_machine": {"core/landing_actions", "core/landing_state"},
}


def _land_lib_imports(path: Path) -> set[str]:
    """The land_lib modules `path` imports, each as a path from `land_lib/` with no suffix."""
    found = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        if node.module != "deploy_tools.land_lib" and not node.module.startswith(
            "deploy_tools.land_lib."
        ):
            continue
        within = node.module.removeprefix("deploy_tools.land_lib").lstrip(".")
        package = within.replace(".", "/")
        if (LIB / package).is_dir():
            found.update(f"{package}/{a.name}".lstrip("/") for a in node.names)
        else:
            found.add(package)
    return found


def _present() -> set[str]:
    return {p.relative_to(LIB).with_suffix("").as_posix() for p in LIB.rglob("*.py")}


def _impure_imports(path: Path) -> set[str]:
    """What `path` imports that is neither a module under `core/` nor in `PURE_STDLIB`."""
    found = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return {
        name
        for name in found
        if name not in PURE_STDLIB
        and name != "deploy_tools.land_lib.core"
        and not name.startswith("deploy_tools.land_lib.core.")
    }


def test_the_module_set_is_exactly_what_is_on_disk():
    """`==`, not `<=`: a module added without being checked must fail."""
    assert MODULES == _present(), (
        f"missing from MODULES: {sorted(_present() - MODULES)}; "
        f"listed but absent: {sorted(MODULES - _present())}"
    )


def test_no_module_imports_outside_its_allowed_set():
    """Iterates what is ON DISK, so a new module with no ALLOWED entry is a KeyError."""
    present = _present()
    assert present, "the glob matched nothing -- LIB is wrong, not the tree"
    for name in sorted(present):
        got = _land_lib_imports(LIB / f"{name}.py")
        assert name in ALLOWED, f"{name} has no ALLOWED entry"
        assert got <= ALLOWED[name], f"{name} imports {sorted(got - ALLOWED[name])}"


def test_the_guard_would_catch_a_phase_importing_the_pipeline(tmp_path):
    """The reject half: the reader must see both import forms."""
    p = tmp_path / "x.py"
    p.write_text(
        "from deploy_tools.land_lib import pipeline\nfrom deploy_tools.land_lib.deploy import deploy_phase\n"
    )
    assert _land_lib_imports(p) == {"pipeline", "deploy"}


def test_core_imports_nothing_that_does_io():
    present = _present()
    assert CORE <= present, f"missing under core/: {sorted(CORE - present)}"
    for name in sorted(n for n in present if n.startswith("core/")):
        assert not _impure_imports(LIB / f"{name}.py"), name


def test_the_guards_would_catch_a_core_module_importing_turn_or_os(tmp_path):
    p = tmp_path / "x.py"
    p.write_text(
        "import os\n"
        "from dataclasses import dataclass\n"
        "from deploy_tools.land_lib import turn\n"
        "from deploy_tools.land_lib.core.queue_state import Place\n"
        "from deploy_tools.land_lib.core import invariants\n"
    )
    assert _impure_imports(p) == {"os", "deploy_tools.land_lib"}
    got = _land_lib_imports(p)
    assert got == {"turn", "core/queue_state", "core/invariants"}
    assert got - ALLOWED["core/queue_machine"] == {"turn", "core/invariants"}
