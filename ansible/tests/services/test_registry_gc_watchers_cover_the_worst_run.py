"""Both registry-gc watchers allow the run's derived worst case past its schedule.

registry-gc.sh collects the ghcr-mirror's store and then the dockerhub-mirror's, and pings
healthchecks.io and pushes the Kuma `Registry GC` tile only when it ends. Each phase is bounded
by the pod-exit wait, the unfinished Job's delete wait, the Job deadline, the rollout wait and
that mirror's warm budget, and the run waits up to `flock -w` for the lock first. The two
allowances are literals, because neither monitor-bridge's nor uptime-kuma's render can read a
setup role's defaults. This guard derives the worst case from the rendered script, and holds
each literal to the whole hour that covers it (#6569).

Run: uv run pytest ansible/tests/services/test_registry_gc_watchers_cover_the_worst_run.py
"""

import math
import re

from _helpers import REPO
from _kuma_entities import _entities
from _shell_render import rendered_shell_text
from lib.estate import role_defaults

BRIDGE_ROLE = REPO / "ansible/roles/k8s/monitor-bridge"
WEEK_S = 7 * 24 * 3600

# Each bounded wait one phase runs once, by the script line that carries it.
_PHASE_WAITS = {
    "pod-exit wait": r"wait --for=delete pod [^\n]*--timeout=(\d+)s",
    "Job-delete wait": r"delete job [^\n]*\\\n[^\n]*--timeout=(\d+)s",
    "Job deadline": r"wait --for=condition=complete [^\n]*\\\n[^\n]*--timeout=(\d+)s",
    "rollout wait": r"rollout status [^\n]*--timeout=(\d+)s",
}


def _bounds(pattern: str, script: str, name: str) -> list[int]:
    found = [int(n) for n in re.findall(pattern, script)]
    assert found, f"{name}: no bounded wait matches {pattern!r}"
    return found


def worst_case_s(script: str) -> int:
    """The longest a run can take from its cron start to its push, in seconds."""
    per_phase = 0
    for name, pattern in _PHASE_WAITS.items():
        waits = _bounds(pattern, script, name)
        assert len(waits) == 1, f"{name}: expected one bounded wait, found {waits}"
        per_phase += waits[0]
    # One warm budget per mirror phase: the ghcr-mirror's, then the dockerhub-mirror's.
    warms = _bounds(r"WARM_BUDGET_S=(\d+)", script, "warm budget")
    assert len(warms) == 2, f"expected two mirror phases, found warm budgets {warms}"
    (lock,) = _bounds(r"flock -w (\d+)", script, "lock wait")
    return lock + sum(per_phase + warm for warm in warms)


def allowance_problems(name: str, allowance: int, worst: int) -> list[str]:
    """Empty when `allowance` is the whole hour that covers `worst`."""
    want = math.ceil(worst / 3600) * 3600
    if allowance == want:
        return []
    return [
        f"{name}: allows {allowance}s past the schedule, the worst run needs {want}s"
    ]


def _script() -> str:
    return rendered_shell_text("setup", "k8s_host_files", "registry-gc.sh.j2")


def _worst() -> int:
    return worst_case_s(_script())


def test_the_worst_case_sums_both_phases_and_the_lock():
    # 60 + (120 + 120 + 1200 + 180) * 2 + 1800 + 900 at the defaults this guard was written at.
    assert _worst() >= 6000


def test_the_healthchecks_grace_covering_the_worst_run_is_clean():
    rows = role_defaults(BRIDGE_ROLE)["monitor_bridge_healthchecks_expected"]
    (grace,) = [r["grace"] for r in rows if r["slug"] == "registry-gc"]
    assert allowance_problems("healthchecks.io grace", grace, _worst()) == []


def test_the_kuma_interval_covering_the_week_and_the_worst_run_is_clean():
    interval = _entities()["registry-gc.json"]["interval"]
    assert allowance_problems("Kuma interval", interval - WEEK_S, _worst()) == []


def test_a_warm_budget_raised_past_the_grace_is_flagged():
    script = _script().replace("WARM_BUDGET_S=900", "WARM_BUDGET_S=2700")
    assert script != _script()
    worst = worst_case_s(script)
    assert allowance_problems("grace", 7200, worst) == [
        "grace: allows 7200s past the schedule, the worst run needs 10800s"
    ]
