"""The SessionStart banner runs its probes at once, under one total deadline (#6153).

Every probe is a fake passed through main()'s keyword seams, for the reason
`test_session_health.py`'s `_run_main` gives.
"""

import importlib.util
import json
import os
import threading
from pathlib import Path

_HOOK = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "session-health.py"
)
_spec = importlib.util.spec_from_file_location("session_health_deadline", _HOOK)
assert _spec and _spec.loader, "spec_from_file_location found no loader"
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def _probes(**overrides):
    probes = {
        "read_payload": lambda: {"source": "startup"},
        "target_problems": lambda: [],
        "master_moved_problems": lambda: [],
        "parked_deployer_problems": lambda: [],
        "other_live_sessions": lambda cwd: [],
        "unlabelled_worktree_lines": lambda cwd: [],
        "stale_worktree_lines": lambda: [],
        "remote_fanout_lines": lambda: [],
    }
    probes.update(overrides)
    return probes


def test_the_probes_run_at_the_same_time(capsys):
    # Each probe waits for the other: run one after another, the first never returns and
    # the barrier times out, which leaves both sections empty.
    barrier = threading.Barrier(2, timeout=5)

    def target_problems():
        barrier.wait()
        return ["  ✗ target loki [loki:3100] down"]

    def stale_worktree_lines():
        barrier.wait()
        return ["  old-thing — worktree-old-thing merged"]

    probes = _probes(
        target_problems=target_problems, stale_worktree_lines=stale_worktree_lines
    )
    assert _mod.main(**probes) == 0
    out = capsys.readouterr().out
    assert "loki" in out
    assert "old-thing" in out


def test_a_probe_past_the_deadline_is_named_and_the_rest_still_print(capsys):
    never = threading.Event()

    def hung():
        never.wait()
        return ["  never printed"]

    probes = _probes(
        target_problems=lambda: ["  ✗ target loki [loki:3100] down"],
        stale_worktree_lines=hung,
    )
    try:
        assert _mod.main(deadline=0.2, **probes) == 0
    finally:
        never.set()
    out = capsys.readouterr().out
    assert "loki" in out
    assert "the removable-worktree survey did not finish within 0.2s" in out
    assert "never printed" not in out


def test_the_total_deadline_fits_inside_the_hook_budget():
    settings = json.loads(
        Path(_HOOK).parent.parent.joinpath("settings.json").read_text(encoding="utf-8")
    )
    budgets = [
        hook["timeout"]
        for group in settings["hooks"]["SessionStart"]
        for hook in group["hooks"]
        if hook["command"].endswith("run-hook.sh session-health")
    ]
    assert budgets, "settings.json registers no session-health SessionStart hook"
    # The rest of the budget pays for `uv run` starting the interpreter and for printing.
    assert _mod.TOTAL_DEADLINE_S <= min(budgets) - 4
