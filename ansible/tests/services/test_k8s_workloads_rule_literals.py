"""Each literal in observability's k8s-workloads rules equals the bridge value it was ported from.

`files/rules/k8s-workloads.yaml` runs in shadow beside monitor-bridge's `k8s_workloads` check,
and M2's soak compares the two. A rule whose floor, hold or window drifted from the bridge's
would make that comparison measure the drift instead of the port. So, until M2's PR 4a deletes
the bridge arms, each literal is held to its bridge source:

- the census floors equal `K8S_MIN_WORKLOADS` and `K8S_MIN_DAEMONSETS`;
- each `for:` equals its streak times `INTERVAL`, with N = 1 for an arm the bridge pages on
  one sample;
- the crash-loop `1h`, `3` and `30m` equal `K8S_RESTART_WINDOW`, `K8S_RESTART_MAX` and
  `K8S_RESTART_RECENT_WINDOW`.

A bridge value comes from the rendered pod environment when it renders one, and otherwise from the
Python default in `bridge/config_cluster.py`, which is what the pod runs with.

Two checks survive PR 4a, because they hold the rule to the estate rather than to the bridge:

- the DaemonSet floor equals the DaemonSets the roles in `containers_list` render, plus the
  add-on DaemonSets Longhorn and MetalLB bring;
- the crash-loop recency window is at least 30m, the floor the `DECIDED 30m` marker in
  `bridge/config_cluster.py` derives from 15-19 minute restart spacing, and shorter than the
  evidence window.

Run: uv run pytest ansible/tests/services/test_k8s_workloads_rule_literals.py
"""

import ast
import re

from _helpers import K8S_ROLES
from _k8s_render import rendered_docs
from _kuma_entities import bridge_env
from lib import yaml_fast

RULES = K8S_ROLES / "observability" / "files" / "rules" / "k8s-workloads.yaml"
CONFIG_CLUSTER = K8S_ROLES / "monitor-bridge" / "files" / "bridge" / "config_cluster.py"

# Each held alert and the bridge streak its `for:` mirrors. None is an arm the bridge pages on
# one sample, so N = 1.
HOLDS = {
    "K8sDeploymentReplicasUnavailable": "K8S_WORKLOADS_CONSECUTIVE",
    "K8sDeploymentZeroAvailable": "K8S_ZERO_AVAILABLE_CONSECUTIVE",
    "K8sDeploymentRolloutStalled": "K8S_ROLLOUT_STALL_CONSECUTIVE",
    "K8sDaemonSetUnavailable": None,
    "K8sDeploymentSeriesMissing": None,
    "K8sDaemonSetSeriesMissing": None,
    "K8sExtendedResourceMissing": None,
}

# Each census alert, the family it counts, and the bridge floor it mirrors.
FLOORS = {
    "K8sDeploymentSeriesMissing": (
        "kube_deployment_status_replicas_unavailable",
        "K8S_MIN_WORKLOADS",
    ),
    "K8sDaemonSetSeriesMissing": (
        "kube_daemonset_status_number_unavailable",
        "K8S_MIN_DAEMONSETS",
    ),
}

CRASH_LOOP = "K8sContainerCrashLooping"
RESTART_INCREASE = re.compile(
    r"increase\(kube_pod_container_status_restarts_total\[(\w+)\]\)\s*>\s*(\d+)"
)
# The `DECIDED 30m` floor in bridge/config_cluster.py: the worst observed restart spacing was
# 15-19 minutes, and a recency window inside it goes up in the gaps and flaps.
RECENCY_FLOOR_S = 30 * 60

# The DaemonSets the setup/k3s role's add-ons bring, which no role in containers_list renders.
# Longhorn names its engine-image DaemonSet after the image's hash (engine-image-ei-<hash>), so
# it is listed by that prefix.
ADDON_DAEMONSETS = (
    "longhorn-manager",
    "longhorn-csi-plugin",
    "engine-image-ei-",
    "speaker",
)
# A rendered DaemonSet the count must find. dri-device-plugin is also the extended-resource
# alert's subject, so a render that lost it would hide two things.
MUST_RENDER = "dri-device-plugin"

DURATION = re.compile(r"(\d+)([smhd])")
UNIT_S = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def seconds(duration: str) -> int:
    """A Prometheus duration such as `15m` or `1h30m`, or a bare count of seconds, in seconds."""
    if duration.isdigit():
        return int(duration)
    parts = DURATION.findall(duration)
    assert parts and "".join(n + u for n, u in parts) == duration, duration
    return sum(int(n) * UNIT_S[u] for n, u in parts)


def rule_alerts() -> dict[str, dict]:
    """Every alerting rule in k8s-workloads.yaml, by name."""
    doc = yaml_fast.safe_load(RULES.read_text())
    return {
        rule["alert"]: rule
        for group in doc["groups"]
        for rule in group["rules"]
        if "alert" in rule
    }


def _python_defaults() -> dict[str, str]:
    """The literal default of every `_env`/`_int`/`_num` read in bridge/config_cluster.py."""
    found = {}
    for node in ast.walk(ast.parse(CONFIG_CLUSTER.read_text())):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"_env", "_int", "_num"}
            and len(node.args) > 1
        ):
            key, default = node.args[0], node.args[1]
            if isinstance(key, ast.Constant) and isinstance(default, ast.Constant):
                found[str(key.value)] = str(default.value)
    return found


def bridge_values() -> dict[str, str]:
    """The bridge's value for each key the rules mirror: rendered when the pod environment
    carries it, else the Python default the pod runs with."""
    keys = {
        "INTERVAL",
        *(k for k in HOLDS.values() if k),
        *(key for _family, key in FLOORS.values()),
        "K8S_RESTART_WINDOW",
        "K8S_RESTART_MAX",
        "K8S_RESTART_RECENT_WINDOW",
    }
    values = {**_python_defaults(), **bridge_env()}
    missing = keys - values.keys()
    assert not missing, (
        f"neither the pod environment nor config_cluster.py sets {missing}"
    )
    return {key: values[key] for key in keys}


def hold_problems(alerts: dict[str, dict], values: dict[str, str]) -> list[str]:
    """A problem for each held alert whose `for:` is not its streak times INTERVAL."""
    problems = []
    interval = seconds(values["INTERVAL"])
    for name, streak in HOLDS.items():
        if name not in alerts:
            problems.append(f"{name}: no such alert in {RULES.name}")
            continue
        cycles = int(values[streak]) if streak else 1
        held = alerts[name].get("for")
        if held is None or seconds(held) != cycles * interval:
            problems.append(
                f"{name}: for is {held!r}, and the bridge holds it {cycles} x "
                f"{interval}s = {cycles * interval}s ({streak or 'no streak'})"
            )
    return problems


def floor_problems(alerts: dict[str, dict], values: dict[str, str]) -> list[str]:
    """A problem for each census alert whose `count(...) < N` floor is not the bridge's."""
    problems = []
    for name, (family, key) in FLOORS.items():
        expr = str(alerts.get(name, {}).get("expr", ""))
        floors = re.findall(rf"count\({family}\)\s*<\s*(\d+)", expr)
        if floors != [values[key]]:
            problems.append(
                f"{name}: counts {family} against {floors}, and the bridge's {key} is "
                f"{values[key]}"
            )
    return problems


def crash_loop_windows(expr: str) -> tuple[tuple[str, int], tuple[str, int]] | None:
    """The crash-loop rule's (evidence window, max) and (recency window, 0), or None.

    The join writes the condition twice, once per side of its `or`, so both copies must agree.
    """
    found = {(w, int(n)) for w, n in RESTART_INCREASE.findall(expr)}
    recent = [pair for pair in found if pair[1] == 0]
    evidence = [pair for pair in found if pair[1] > 0]
    if len(found) != 2 or len(recent) != 1 or len(evidence) != 1:
        return None
    return evidence[0], recent[0]


def crash_loop_problems(alerts: dict[str, dict], values: dict[str, str]) -> list[str]:
    """A problem unless the crash-loop windows and max equal the bridge's K8S_RESTART_*."""
    windows = crash_loop_windows(str(alerts.get(CRASH_LOOP, {}).get("expr", "")))
    if windows is None:
        return [
            f"{CRASH_LOOP}: expected one `[W]) > N` evidence test and one `[R]) > 0` recency "
            "test, written the same in both halves of the join"
        ]
    (window, most), (recent, _zero) = windows
    want = (
        values["K8S_RESTART_WINDOW"],
        int(values["K8S_RESTART_MAX"]),
        values["K8S_RESTART_RECENT_WINDOW"],
    )
    if (window, most, recent) != want:
        return [
            f"{CRASH_LOOP}: reads ({window}, > {most}, {recent}), and the bridge's "
            f"K8S_RESTART_WINDOW, K8S_RESTART_MAX and K8S_RESTART_RECENT_WINDOW are {want}"
        ]
    return []


def recency_problems(alerts: dict[str, dict]) -> list[str]:
    """A problem unless the recency window is at least 30m and shorter than the evidence."""
    windows = crash_loop_windows(str(alerts.get(CRASH_LOOP, {}).get("expr", "")))
    if windows is None:
        return [f"{CRASH_LOOP}: its two restart windows cannot be read"]
    (window, _most), (recent, _zero) = windows
    problems = []
    if seconds(recent) < RECENCY_FLOOR_S:
        problems.append(
            f"{CRASH_LOOP}: the recency window {recent} is under 30m, inside the 15-19 minute "
            "restart spacing, so the alert would clear and refire between restarts"
        )
    if seconds(recent) >= seconds(window):
        problems.append(
            f"{CRASH_LOOP}: the recency window {recent} is not shorter than the evidence "
            f"window {window}, so it gates nothing"
        )
    return problems


def rendered_daemonsets(docs) -> set[str]:
    """The names of every DaemonSet the roles in containers_list render."""
    return {
        doc["metadata"]["name"]
        for _role, _tpl, doc in docs
        if doc.get("kind") == "DaemonSet"
    }


def daemonset_floor_problems(alerts: dict[str, dict], rendered: set[str]) -> list[str]:
    """A problem unless the DaemonSet floor counts the rendered DaemonSets plus the add-ons."""
    if MUST_RENDER not in rendered:
        return [
            f"the render produced no {MUST_RENDER} DaemonSet, so the count below reads too "
            f"little: found {sorted(rendered)}"
        ]
    family, _key = FLOORS["K8sDaemonSetSeriesMissing"]
    expr = str(alerts.get("K8sDaemonSetSeriesMissing", {}).get("expr", ""))
    floors = re.findall(rf"count\({family}\)\s*<\s*(\d+)", expr)
    estate = len(rendered) + len(ADDON_DAEMONSETS)
    if floors != [str(estate)]:
        return [
            f"K8sDaemonSetSeriesMissing: the floor is {floors}, and the estate runs {estate} "
            f"DaemonSets: {len(rendered)} rendered ({sorted(rendered)}) plus the add-ons "
            f"{list(ADDON_DAEMONSETS)}. Move the floor with the estate."
        ]
    return []


def test_every_hold_equals_its_bridge_streak_is_clean():
    assert hold_problems(rule_alerts(), bridge_values()) == []


def test_a_hold_off_its_bridge_streak_is_flagged():
    alerts = rule_alerts()
    alerts["K8sDeploymentZeroAvailable"] = {
        **alerts["K8sDeploymentZeroAvailable"],
        "for": "5m",
    }
    problems = hold_problems(alerts, bridge_values())
    assert len(problems) == 1 and "K8sDeploymentZeroAvailable" in problems[0], problems


def test_a_missing_held_alert_is_flagged():
    alerts = rule_alerts()
    del alerts["K8sDaemonSetUnavailable"]
    problems = hold_problems(alerts, bridge_values())
    assert any("K8sDaemonSetUnavailable: no such alert" in p for p in problems), (
        problems
    )


def test_the_census_floors_equal_the_bridge_floors_is_clean():
    assert floor_problems(rule_alerts(), bridge_values()) == []


def test_a_census_floor_off_the_bridge_floor_is_flagged():
    values = {**bridge_values(), "K8S_MIN_WORKLOADS": "6"}
    problems = floor_problems(rule_alerts(), values)
    assert len(problems) == 1 and "K8S_MIN_WORKLOADS" in problems[0], problems


def test_the_crash_loop_windows_equal_the_bridge_windows_is_clean():
    assert crash_loop_problems(rule_alerts(), bridge_values()) == []


def test_a_crash_loop_window_off_the_bridge_window_is_flagged():
    values = {**bridge_values(), "K8S_RESTART_RECENT_WINDOW": "45m"}
    problems = crash_loop_problems(rule_alerts(), values)
    assert len(problems) == 1 and "K8S_RESTART_RECENT_WINDOW" in problems[0], problems


def test_one_half_of_the_join_with_another_window_is_flagged():
    alerts = rule_alerts()
    expr = alerts[CRASH_LOOP]["expr"]
    alerts[CRASH_LOOP] = {**alerts[CRASH_LOOP], "expr": expr.replace("[1h]", "[2h]", 1)}
    problems = crash_loop_problems(alerts, bridge_values())
    assert problems and "both halves" in problems[0], problems


def test_the_recency_window_sits_between_the_floor_and_the_evidence_is_clean():
    assert recency_problems(rule_alerts()) == []


def test_a_recency_window_inside_the_restart_spacing_is_flagged():
    alerts = rule_alerts()
    expr = alerts[CRASH_LOOP]["expr"].replace("[30m]", "[15m]")
    alerts[CRASH_LOOP] = {**alerts[CRASH_LOOP], "expr": expr}
    problems = recency_problems(alerts)
    assert len(problems) == 1 and "under 30m" in problems[0], problems


def test_the_daemonset_floor_counts_the_estate_is_clean():
    rendered = rendered_daemonsets(rendered_docs())
    assert MUST_RENDER in rendered
    assert daemonset_floor_problems(rule_alerts(), rendered) == []


def test_a_daemonset_the_floor_does_not_count_is_flagged():
    rendered = rendered_daemonsets(rendered_docs())
    one_fewer = rendered - {sorted(rendered - {MUST_RENDER})[0]}
    problems = daemonset_floor_problems(rule_alerts(), one_fewer)
    assert len(problems) == 1 and "Move the floor" in problems[0], problems
