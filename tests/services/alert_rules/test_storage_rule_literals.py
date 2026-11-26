"""The storage rules' literals that another file declares equal that file's value.

`files/rules/storage.yaml` is plain YAML, so each threshold is a literal. A threshold of the
rule's own carries its reason as a `DECIDED:` marker in the rules file. The tests here hold the
literals another file owns: the Longhorn state allow-list, valheim's transient, jellyfin's
snapshot cap and k3s-host-checks' disk floor, plus the claims the fullness page must not drop.

Run: uv run pytest tests/services/alert_rules/test_storage_rule_literals.py
"""

import re

from _helpers import K8S_ROLES
from lib import yaml_fast
from lib.repo_paths import K3S_HOST_CHECKS_DEFAULTS

RULES = K8S_ROLES / "observability" / "files" / "rules" / "storage.yaml"
ROBUSTNESS = K8S_ROLES / "monitor-bridge" / "files" / "longhorn_robustness.py"
GIB = 1024**3


def safe_robustness() -> str:
    """The `a|b` regex of SAFE_ROBUSTNESS, the allow-list the runbook gates and the rule share."""
    [members] = re.findall(
        r"SAFE_ROBUSTNESS = frozenset\(\{([^}]*)\}\)", ROBUSTNESS.read_text()
    )
    return "|".join(sorted(re.findall(r'"(\w+)"', members)))


def rules_by_name(text: str) -> dict[str, dict]:
    """The storage group's rules, keyed by alert name."""
    [group] = yaml_fast.safe_load(text)["groups"]
    return {rule["alert"]: rule for rule in group["rules"]}


def allow_list_problems(text: str) -> list[str]:
    """`LonghornVolumeDegraded` must skip exactly the states SAFE_ROBUSTNESS names."""
    fragment = f'state!~"{safe_robustness()}"'
    if fragment in rule_expr("LonghornVolumeDegraded", text):
        return []
    return [f"LonghornVolumeDegraded: expr lacks `{fragment}`"]


def test_the_degraded_rule_skips_the_states_the_runbook_calls_safe():
    assert allow_list_problems(RULES.read_text()) == []


def test_a_state_dropped_from_the_allow_list_is_flagged():
    text = RULES.read_text().replace('"healthy|unknown"', '"healthy"')
    assert allow_list_problems(text) == [
        f'LonghornVolumeDegraded: expr lacks `state!~"{safe_robustness()}"`'
    ]


# Nothing reclaims these claims, so the fullness page is their only bound.
UNCOLLECTED_CLAIMS = frozenset({"registry-data"})


def excluded_uncollected(text: str) -> list[str]:
    """Each uncollected claim `PVCFull` drops from the fullness page."""
    # The expression filters twice, the available and the capacity series, so read both.
    exclusions = re.findall(
        r'persistentvolumeclaim!~"([^"]*)"', rule_expr("PVCFull", text)
    )
    assert exclusions, (
        "PVCFull no longer excludes any claim, so nothing here pins its list"
    )
    dropped = {claim for exclusion in exclusions for claim in exclusion.split("|")}
    return sorted(UNCOLLECTED_CLAIMS & dropped)


def test_no_uncollected_claim_is_excluded_from_the_fullness_page():
    # fact: k8s/roles/registry/CLAUDE.md#Notable
    assert excluded_uncollected(RULES.read_text()) == []


def test_an_uncollected_claim_in_the_exclusion_is_flagged():
    text = RULES.read_text().replace(
        'persistentvolumeclaim!~"media-data"',
        'persistentvolumeclaim!~"media-data|registry-data"',
    )
    assert excluded_uncollected(text) == ["registry-data"]


def rule_expr(alert: str, text: str | None = None) -> str:
    rules = rules_by_name(RULES.read_text() if text is None else text)
    return " ".join(rules[alert]["expr"].split())


def role_default(role: str, name: str) -> str:
    """A role default read as text, so a value that moves into a template fails loudly."""
    text = (K8S_ROLES / role / "defaults" / "main.yml").read_text()
    match = re.search(rf"^{name}:\s*(\S+)\s*$", text, re.M)
    assert match, f"{role} no longer declares {name} as a plain value"
    return match.group(1)


def valheim_floor_problems(text: str) -> list[str]:
    """The tree-derived half, by NAME: the valheim role declares the transient its updater
    stages, and `PVCBelowFreeFloor` must carry that claim and number. The rule's `DECIDED:`
    marker says why the floor is the transient rather than a gap under the percentage.
    """
    claim = role_default("valheim", "valheim_k8s_server_claim")
    transient = int(
        role_default("valheim", "valheim_k8s_server_update_transient_bytes")
    )
    expr = rule_expr("PVCBelowFreeFloor", text)
    problems = [
        f"PVCBelowFreeFloor: expr lacks `{fragment}`"
        for fragment in (f'persistentvolumeclaim="{claim}"', f"< {transient}")
        if fragment not in expr
    ]
    # The floor must be reachable: a transient larger than the claim would page forever.
    size = re.fullmatch(r"(\d+)Gi", role_default("valheim", "valheim_k8s_server_size"))
    assert size, "valheim_k8s_server_size is no longer a plain Gi value"
    if transient >= int(size.group(1)) * GIB:
        problems.append("the transient does not fit in the claim")
    return problems


def test_the_valheim_floor_is_the_roles_own_transient():
    assert valheim_floor_problems(RULES.read_text()) == []


def test_a_floor_that_leaves_the_roles_transient_behind_is_flagged():
    text = RULES.read_text().replace("< 3221225472", "< 2147483648")
    assert valheim_floor_problems(text) == [
        "PVCBelowFreeFloor: expr lacks `< 3221225472`"
    ]


def roles_that_cap_a_volume() -> dict[str, str]:
    """Role name -> the claim it caps, derived from the tree rather than from the rule.

    A role caps a volume by writing `spec.snapshotMaxSize` (the Job in
    k8s/roles/jellyfin/templates/verify/cap.yaml.j2 is the only one). The census matches the
    FIELD NAME anywhere in a k8s role's verify Jobs, minus the roles that only READ
    it, rather than jellyfin's patch payload: a future role capping a volume through
    `kubernetes.core.k8s` or `--type=json` writes the same field with different syntax, and a
    payload-shaped pattern would miss it and pass. The claim comes from the role's own
    `<role>_k8s_claim` default, which is the variable the write resolves its volume through.
    """
    # A role added here needs the reason in writing.
    readers = {"volume-snapshot", "volume-revert"}
    census = {}
    writers = sorted(K8S_ROLES.glob("*/templates/verify/*.j2"))
    for source in writers:
        role_dir = next(p for p in source.parents if p.parent == K8S_ROLES)
        if "snapshotMaxSize" not in source.read_text() or role_dir.name in readers:
            continue
        role = role_dir.name
        census[role] = role_default(role, f"{role.replace('-', '_')}_k8s_claim")
    return census


def snapshot_cap_problems(text: str) -> list[str]:
    capped = roles_that_cap_a_volume()
    # By name, not count: jellyfin is the one role that caps a volume.
    assert capped.get("jellyfin") == "jellyfin-config", capped
    problems = [
        f'{alert}: expr lacks `pvc="{claim}"`'
        for alert in ("LonghornSnapshotNearCap", "LonghornSnapshotCapUnmonitored")
        for claim in capped.values()
        if f'pvc="{claim}"' not in rule_expr(alert, text)
    ]
    # The cap is 2 x the PVC size, the smallest Longhorn accepts.
    size = re.fullmatch(r"(\d+)Gi", role_default("jellyfin", "jellyfin_k8s_size"))
    assert size, "jellyfin_k8s_size is no longer a plain Gi value"
    cap = int(size.group(1)) * 2 * GIB
    if f">= {cap} *" not in rule_expr("LonghornSnapshotNearCap", text):
        problems.append(f"LonghornSnapshotNearCap: expr lacks `>= {cap} *`")
    return problems


def test_every_volume_a_role_caps_is_watched_by_the_snapshot_rules():
    assert snapshot_cap_problems(RULES.read_text()) == []


def test_a_snapshot_rule_that_watches_another_claim_is_flagged():
    text = RULES.read_text().replace('pvc="jellyfin-config"', 'pvc="other-config"')
    assert snapshot_cap_problems(text) == [
        'LonghornSnapshotNearCap: expr lacks `pvc="jellyfin-config"`',
        'LonghornSnapshotCapUnmonitored: expr lacks `pvc="jellyfin-config"`',
    ]


def test_a_cap_that_no_longer_matches_the_claim_size_is_flagged():
    text = RULES.read_text().replace(">= 17179869184 *", ">= 8589934592 *")
    assert snapshot_cap_problems(text) == [
        "LonghornSnapshotNearCap: expr lacks `>= 17179869184 *`"
    ]


def disk_floor_problems(text: str) -> list[str]:
    """`LonghornDiskOverSchedulingFloor` must page at k3s-host-checks' `k3s_disk_max_pct`.

    Both encode Longhorn's replica scheduling floor (100 minus its 25%
    storage-minimal-available-percentage): disk-health.sh pages daniel-box's root at it, and
    the rule pages each node's Longhorn disk at it. One moved alone would page the two
    nodes at different lines.
    """
    match = re.search(
        r"^k3s_disk_max_pct:\s*(\d+)\s*$", K3S_HOST_CHECKS_DEFAULTS.read_text(), re.M
    )
    assert match, "k3s-host-checks no longer declares k3s_disk_max_pct as a plain value"
    floor = int(match.group(1)) / 100
    expr = rule_expr("LonghornDiskOverSchedulingFloor", text)
    threshold = re.fullmatch(r".*\) > (\d+(?:\.\d+)?)", expr)
    assert threshold, (
        f"LonghornDiskOverSchedulingFloor no longer ends `) > <number>`: {expr}"
    )
    if float(threshold.group(1)) != floor:
        return [
            f"LonghornDiskOverSchedulingFloor: pages above {threshold.group(1)}, not {floor}"
        ]
    return []


def test_the_longhorn_disk_floor_is_the_host_checks_floor():
    assert disk_floor_problems(RULES.read_text()) == []


def test_a_disk_floor_that_leaves_the_host_checks_floor_behind_is_flagged():
    # 0.755 keeps `> 0.75` as a substring, so only a numeric comparison catches it.
    text = RULES.read_text().replace(
        "longhorn_disk_capacity_bytes) > 0.75", "longhorn_disk_capacity_bytes) > 0.755"
    )
    assert disk_floor_problems(text) == [
        "LonghornDiskOverSchedulingFloor: pages above 0.755, not 0.75"
    ]
