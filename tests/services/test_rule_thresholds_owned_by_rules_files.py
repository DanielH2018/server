"""A Prometheus rule threshold is owned by its rules file, and the tests restate none of it.

Each threshold the deleted literal tests pinned carries its rationale as a `# DECIDED:` comment
in its rule's span in `k8s/roles/observability/files/rules/`, or in `files/rule-tables/` for a
group `scripts/dev/gen_host_rules.py` renders, so review reads a threshold edit against the
marker beside it (#9023). The rule-literal tests that only compared literals are
gone, and the structural ones carry no rationale of their own. The per-batch routing modules
are replaced by one table in `test_alertmanager_routing.py`, which names every rules file the
batch modules walked.

Run: uv run pytest tests/services/test_rule_thresholds_owned_by_rules_files.py
"""

import re

from _helpers import K8S_ROLES, REPO
from _ratchet_census import tracked_files
from test_alertmanager_routing import ROUTED

RULES_DIR = K8S_ROLES / "observability" / "files" / "rules"
TABLES_DIR = K8S_ROLES / "observability" / "files" / "rule-tables"
DECIDED = re.compile(r"#\s*DECIDED:")
ALERT_LINE = re.compile(r"^\s*- alert:\s*(\S+)\s*$")
# A rationale written into a test: a docstring line or a comment that opens with the marker.
RATIONALE_IN_TEST = re.compile(r"^\s*(#\s*)?DECIDED:", re.MULTILINE)

# The thresholds the literal-only tests pinned, by rules file.
PINNED = {
    "edge.yaml": [
        "CertExpiringSoon",
        "TraefikBackend5xx",
        "TraefikBackendSlow",
        "TraefikEntrypoint404Flood",
        "TraefikRouter421",
    ],
    "container-checks.yaml": ["CAdvisorCoverageShortfall"],
    "hosts.yaml": [
        "HostDiskFull",
        "HostMemoryHigh",
        "HostTemperatureHot",
        "HostCoverageShortfall",
        "HostTemperatureCoverageShortfall",
        "UPSBatteryLow",
        "UPSRuntimeLow",
        "PiLoadHigh",
        "PiMemoryLow",
        "PiDiskFull",
    ],
    "storage.yaml": [
        "PVCFull",
        "PVCBelowFreeFloor",
        "PVCCoverageShortfall",
        "LonghornSnapshotNearCap",
        "EtcdDBNearQuota",
    ],
}

# The rules files the nine `test_alertmanager_routing_<batch>.py` modules walked.
BATCH_RULE_FILES = [
    "bridge-exporter.yaml",
    "extra-probes.yaml",
    "gitops-checks.yaml",
    "host-checks.yaml",
    "hosts.yaml",
    "initial-setup-checks.yaml",
    "other-setup-checks.yaml",
    "pi-checks.yaml",
    "pod-producers.yaml",
    "pods.yaml",
    "registry.yaml",
]


def source(name: str) -> str:
    """The text a rules file's markers live in: its rule table when a table renders it."""
    table = TABLES_DIR / name
    return (table if table.exists() else RULES_DIR / name).read_text()


def marked_alerts(text: str) -> set[str]:
    """The alerts whose span carries a `# DECIDED:` comment.

    A rule's span is the comment run directly above its `- alert:` line, then every line down
    to the next rule's span.
    """
    lines = text.splitlines()
    starts = []
    for i, line in enumerate(lines):
        if match := ALERT_LINE.match(line):
            start = i
            while start > 0 and lines[start - 1].lstrip().startswith("#"):
                start -= 1
            starts.append((start, match.group(1)))
    ends = [start for start, _ in starts[1:]] + [len(lines)]
    return {
        alert
        for (start, alert), end in zip(starts, ends, strict=True)
        if any(DECIDED.search(line) for line in lines[start:end])
    }


def unmarked() -> dict[str, list[str]]:
    missing = {}
    for name, alerts in PINNED.items():
        marked = marked_alerts(source(name))
        if lacking := [alert for alert in alerts if alert not in marked]:
            missing[name] = lacking
    return missing


def test_every_pinned_threshold_carries_a_decided_marker_on_its_rule():
    assert unmarked() == {}


def test_no_rule_literal_test_keeps_a_threshold_rationale():
    keeping = [
        path
        for path in tracked_files("tests/services/*_rule_literals.py")
        if RATIONALE_IN_TEST.search((REPO / path).read_text())
    ]
    assert keeping == []


def test_the_routing_table_names_every_rules_file_the_batches_walked():
    text = (REPO / "tests/services/test_alertmanager_routing.py").read_text()
    assert [name for name in BATCH_RULE_FILES if f'"{name}"' not in text] == []


# The `for:` holds the deleted literal tests pinned, by rules file.
PINNED_HOLDS = {
    "hosts.yaml": [
        "HostTemperatureHot",
        "HostTemperatureNotReporting",
        "CPUThermalThrottled",
        "UPSOnBattery",
        "UPSBatteryLow",
        "UPSRuntimeLow",
        "UPSReplaceBattery",
    ],
    "storage.yaml": [
        "LonghornVolumeDegraded",
        "LonghornVolumesMissing",
        "LonghornDiskOverSchedulingFloor",
        "PVCCoverageShortfall",
        "LonghornSnapshotNearCap",
        "LonghornSnapshotCapUnmonitored",
        "PVCFull",
        "PVCBelowFreeFloor",
        "PVCFreeFloorUnmonitored",
        "KubeletCSIMountReadOnly",
        "EtcdDBNearQuota",
    ],
    "edge.yaml": ["TraefikRouter421"],
}


def hold_marked_alerts(text: str) -> set[str]:
    """The alerts whose span carries a `# DECIDED:` line that names its `for:` hold."""
    lines = [
        line if "for:" in line else DECIDED.sub("#", line) for line in text.splitlines()
    ]
    return marked_alerts("\n".join(lines))


def unmarked_holds(texts: dict[str, str]) -> dict[str, list[str]]:
    missing = {}
    for name, alerts in PINNED_HOLDS.items():
        marked = hold_marked_alerts(texts[name])
        if lacking := [alert for alert in alerts if alert not in marked]:
            missing[name] = lacking
    return missing


def test_every_pinned_hold_carries_a_decided_marker_on_its_rule():
    texts = {name: source(name) for name in PINNED_HOLDS}
    assert unmarked_holds(texts) == {}


def test_a_hold_whose_marker_is_dropped_is_flagged():
    texts = {name: source(name) for name in PINNED_HOLDS}
    texts["storage.yaml"] = texts["storage.yaml"].replace(
        "# DECIDED: `for: 1m`", "# `for: 1m`"
    )
    assert unmarked_holds(texts) == {"storage.yaml": ["KubeletCSIMountReadOnly"]}


def test_the_routing_table_rows_cover_every_rules_file_the_batches_walked():
    routed = [row[0] for row in ROUTED]
    assert [name for name in BATCH_RULE_FILES if name not in routed] == []


def test_a_hold_with_only_a_threshold_marker_is_flagged():
    texts = {name: source(name) for name in PINNED_HOLDS}
    texts["storage.yaml"] = texts["storage.yaml"].replace(
        "      # DECIDED: `for: 5m`, one bridge cycle, so a single scrape does not page.\n"
        "      - alert: EtcdDBNearQuota",
        "      - alert: EtcdDBNearQuota",
    )
    assert "# DECIDED: 80% of the 2 GiB quota" in texts["storage.yaml"]
    assert unmarked_holds(texts) == {"storage.yaml": ["EtcdDBNearQuota"]}
