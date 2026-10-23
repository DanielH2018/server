"""The storage rules' thresholds equal the monitor-bridge values they port.

`files/rules/storage.yaml` is plain YAML, so each threshold is a literal that mirrors an env value
of the bridge row it shadows (M1 convention 1). A bridge value retuned without the rule would
leave the shadow soak comparing two different checks.

Run: uv run pytest ansible/tests/services/test_storage_rule_literals.py
"""

import re

from _helpers import K8S_ROLES
from _k8s_render import render_pod_env
from lib import yaml_fast

RULES = K8S_ROLES / "observability" / "files" / "rules" / "storage.yaml"
ROBUSTNESS = K8S_ROLES / "monitor-bridge" / "files" / "longhorn_robustness.py"
CYCLE_MINUTES = 5  # the bridge's INTERVAL, which every *_CONSECUTIVE streak counts
ONE_CYCLE = f"{CYCLE_MINUTES}m"  # the hold for a row with no streak


def bridge_env() -> dict[str, str]:
    """The pod env a deploy renders, which is where the bridge reads its thresholds."""
    return {k: str(v) for k, v in render_pod_env("monitor-bridge").items()}


def safe_robustness() -> str:
    """The `a|b` regex of SAFE_ROBUSTNESS, the allow-list the bridge and the runbook gates share."""
    [members] = re.findall(
        r"SAFE_ROBUSTNESS = frozenset\(\{([^}]*)\}\)", ROBUSTNESS.read_text()
    )
    return "|".join(sorted(re.findall(r'"(\w+)"', members)))


def rules_by_name(text: str) -> dict[str, dict]:
    """The storage group's rules, keyed by alert name."""
    [group] = yaml_fast.safe_load(text)["groups"]
    return {rule["alert"]: rule for rule in group["rules"]}


def literal_problems(rules: dict[str, dict], env: dict[str, str], safe: str):
    """Each rule fragment or hold the bridge value says should be present and is not."""
    [floor_claim, floor_bytes] = env["PVC_MIN_FREE"].split("=")
    [cap_claim, cap_bytes] = env["SNAPSHOT_CAPS"].split("=")
    streak = lambda name: f"{int(env[name]) * CYCLE_MINUTES}m"
    expected = {
        "LonghornVolumeDegraded": [f'state!~"{safe}"'],
        "PVCFull": [
            f"> {env['PVC_MAX_PCT']}",
            f'persistentvolumeclaim!~"{env["PVC_EXCLUDE"]}"',
        ],
        "PVCBelowFreeFloor": [
            f'persistentvolumeclaim="{floor_claim}"',
            f"< {floor_bytes}",
        ],
        "PVCFreeFloorUnmonitored": [f'persistentvolumeclaim="{floor_claim}"'],
        "PVCCoverageShortfall": [f"< {env['PVC_MIN_CLAIMS']}"],
        "LonghornSnapshotNearCap": [
            f'pvc="{cap_claim}"',
            f">= {cap_bytes} * {env['SNAPSHOT_CAP_WARN_RATIO']}",
        ],
        "LonghornSnapshotCapUnmonitored": [f'pvc="{cap_claim}"'],
        "EtcdDBNearQuota": [
            f"/ {env['ETCD_DB_QUOTA_BYTES']} >= {env['ETCD_DB_MAX_PCT']}"
        ],
    }
    problems = [
        f"{alert}: expr lacks `{fragment}`"
        for alert, fragments in expected.items()
        for fragment in fragments
        if fragment not in " ".join(rules[alert]["expr"].split())
    ]
    holds = {
        "LonghornVolumeDegraded": streak("LONGHORN_CONSECUTIVE"),
        "LonghornVolumesMissing": streak("LONGHORN_CONSECUTIVE"),
        "PVCCoverageShortfall": streak("PVC_CLAIMS_CONSECUTIVE"),
        "LonghornSnapshotNearCap": streak("SNAPSHOT_CAP_CONSECUTIVE"),
        "LonghornSnapshotCapUnmonitored": streak("SNAPSHOT_CAP_CONSECUTIVE"),
        "PVCFull": ONE_CYCLE,
        "PVCBelowFreeFloor": ONE_CYCLE,
        "PVCFreeFloorUnmonitored": ONE_CYCLE,
        "KubeletCSIMountReadOnly": ONE_CYCLE,
        "EtcdDBNearQuota": ONE_CYCLE,
    }
    problems += [
        f"{alert}: for is not {hold}"
        for alert, hold in holds.items()
        if rules[alert]["for"] != hold
    ]
    return problems


def shipped() -> list[str]:
    return literal_problems(
        rules_by_name(RULES.read_text()), bridge_env(), safe_robustness()
    )


def test_storage_rule_literals_equal_the_bridge_values():
    assert shipped() == []


def test_a_retuned_bridge_value_is_flagged():
    text = RULES.read_text().replace(") > 85", ") > 90")
    assert literal_problems(rules_by_name(text), bridge_env(), safe_robustness()) == [
        "PVCFull: expr lacks `> 85`"
    ]


def test_a_state_dropped_from_the_allow_list_is_flagged():
    text = RULES.read_text().replace('"healthy|unknown"', '"healthy"')
    problems = literal_problems(rules_by_name(text), bridge_env(), safe_robustness())
    assert problems == [
        f'LonghornVolumeDegraded: expr lacks `state!~"{safe_robustness()}"`'
    ]
