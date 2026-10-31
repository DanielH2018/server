"""The storage rules carry the thresholds monitor-bridge's five storage checks carried.

`files/rules/storage.yaml` is plain YAML, so each threshold is a literal. Until M6 batch A5's
Cutover-a2 each one mirrored an env value of the bridge row it shadowed, and this test read the
rendered env. That cutover deleted the rows and their env keys, so the numbers below are the
record of what the bridge carried (M1 convention 1), and a rule edit that drifts from them
fails here. Two of them also derive from other roles (valheim's transient and jellyfin's
snapshot cap), and the last tests hold the rules to those roles.

Run: uv run pytest ansible/tests/services/test_storage_rule_literals.py
"""

import re

from _helpers import K8S_ROLES
from _role_census import task_files
from lib import yaml_fast

RULES = K8S_ROLES / "observability" / "files" / "rules" / "storage.yaml"
ROBUSTNESS = K8S_ROLES / "monitor-bridge" / "files" / "longhorn_robustness.py"
CYCLE_MINUTES = 5  # the bridge's INTERVAL, which every *_CONSECUTIVE streak counted
ONE_CYCLE = f"{CYCLE_MINUTES}m"  # the hold for a row with no streak
GIB = 1024**3

# What the deleted env keys held (`templates/config/env.yaml.j2`), as the shipped rendering
# stringified them.
RETIRED_BRIDGE_ENV = {
    "LONGHORN_CONSECUTIVE": "3",
    "PVC_MAX_PCT": "85",
    "PVC_EXCLUDE": "media-data",
    "PVC_MIN_CLAIMS": "32",
    "PVC_CLAIMS_CONSECUTIVE": "3",
    "PVC_MIN_FREE": "valheim-server=3221225472",
    "SNAPSHOT_CAPS": "jellyfin-config=17179869184",
    "SNAPSHOT_CAP_WARN_RATIO": "0.9",
    "SNAPSHOT_CAP_CONSECUTIVE": "3",
    "ETCD_DB_QUOTA_BYTES": "2147483648",
    "ETCD_DB_MAX_PCT": "80",
}


def bridge_env() -> dict[str, str]:
    """The values the deleted bridge rows read, which the rules must still carry."""
    return dict(RETIRED_BRIDGE_ENV)


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


# Claims nothing reclaims, so the fullness page is their only bound. registry's push store has
# had no garbage collection since D5 retired it, and its verify Job's seed adds one python image
# per `flux_job_image` pin (#5752). The exclusion is read off `PVCFull` itself, the rule that
# pages on a full claim.
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
    # fact: ansible/roles/k8s/registry/CLAUDE.md#Notable
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
    stages, and `PVCBelowFreeFloor` must carry that claim and number.

    DECIDED: 3 GiB even though 85% of the 20Gi claim also breaches at 3 GiB free. The floor is
    sized to the transient, not to open a gap under the percentage: a resize moves what 85%
    means and leaves this where the copy is, which at the 10Gi the incident happened on was
    1.5 G more warning than the percentage gave.
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
    roles/k8s/jellyfin/templates/verify/cap.yaml.j2 is the only one). The census matches the
    FIELD NAME anywhere under a k8s role's tasks or verify Jobs, minus the roles that only READ
    it, rather than jellyfin's patch payload: a future role capping a volume through
    `kubernetes.core.k8s` or `--type=json` writes the same field with different syntax, and a
    payload-shaped pattern would miss it and pass. The claim comes from the role's own
    `<role>_k8s_claim` default, which is the variable the write resolves its volume through.
    """
    # The known readers. volume-snapshot reads the cap to gate its pre-deploy snapshot and never
    # writes one; volume-revert reads the same volumes. A role added here needs the reason in
    # writing, because every entry narrows what this guard can see.
    readers = {"volume-snapshot", "volume-revert"}
    census = {}
    writers = sorted(
        [
            *task_files(K8S_ROLES),
            *K8S_ROLES.glob("*/templates/verify/*.j2"),
        ]
    )
    for source in writers:
        role_dir = next(p for p in source.parents if p.parent == K8S_ROLES)
        if "snapshotMaxSize" not in source.read_text() or role_dir.name in readers:
            continue
        role = role_dir.name
        census[role] = role_default(role, f"{role.replace('-', '_')}_k8s_claim")
    return census


def snapshot_cap_problems(text: str) -> list[str]:
    capped = roles_that_cap_a_volume()
    # Non-vacuity by NAME, not by count: a census that matched nothing would otherwise pass the
    # checks below against an empty set. jellyfin is the one role that caps a volume.
    assert capped.get("jellyfin") == "jellyfin-config", capped
    problems = [
        f'{alert}: expr lacks `pvc="{claim}"`'
        for alert in ("LonghornSnapshotNearCap", "LonghornSnapshotCapUnmonitored")
        for claim in capped.values()
        if f'pvc="{claim}"' not in rule_expr(alert, text)
    ]
    # The cap VALUE, derived the way the jellyfin role derives it: 2 x the PVC size, the
    # smallest cap Longhorn accepts. A PVC resize that moves the cap fails here rather than
    # leaving the rule measuring against a stale number.
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
