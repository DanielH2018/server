"""The k8s-workloads rules' literals that the estate, not the bridge, decides.

The rules were ported from monitor-bridge's `k8s_workloads` check, and until M2's PR 4a each
literal was held to its bridge source. The bridge arms are gone, so the rules are their own
source. Two checks remain, because they hold a rule to the estate:

- the DaemonSet floor equals the DaemonSets the roles in `containers_list` render, plus the
  add-on DaemonSets Longhorn and MetalLB bring;
- the crash-loop recency window is at least 30m, the floor the `DECIDED: 30m` marker in
  `k8s-workloads.yaml` derives from 15-19 minute restart spacing, and shorter than the
  evidence window.

Run: uv run pytest ansible/tests/services/test_k8s_workloads_rule_literals.py
"""

import re

from _helpers import K8S_ROLES
from _k8s_render import rendered_docs
from lib import yaml_fast

RULES = K8S_ROLES / "observability" / "files" / "rules" / "k8s-workloads.yaml"

CRASH_LOOP = "K8sContainerCrashLooping"
RESTART_INCREASE = re.compile(
    r"increase\(kube_pod_container_status_restarts_total\[(\w+)\]\)\s*>\s*(\d+)"
)
# The `DECIDED: 30m` floor in k8s-workloads.yaml: the worst observed restart spacing was
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
    family = "kube_daemonset_status_number_unavailable"
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
