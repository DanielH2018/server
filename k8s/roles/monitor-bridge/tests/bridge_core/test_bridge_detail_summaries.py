"""Each row whose body names its failing members has a summary that reads them.

A body returns `CheckResult(..., details)` and the exporter serves each member as
`homelab_check_detail{check="<row>", detail="<member>"}`. The row's rule summary reads that
series with a template `query`, scoped to its own `check`, and falls back to its category
text when the row exports none: a raising body, or an exporter that predates the series.
promtool's bridge-exporter cases render the expansion; this holds the wiring for every row.

Run: uv run pytest k8s/roles/monitor-bridge/tests/bridge_core/test_bridge_detail_summaries.py
"""

import re

from lib import yaml_fast
from lib.repo_paths import K8S_ROLES

RULES = K8S_ROLES / "observability" / "files" / "rules" / "bridge-exporter.yaml"

DETAIL_ROWS = frozenset(
    {
        "arr_queue",
        "b2_storage",
        "bazarr",
        "cloudflare_ips_drift",
        "discord",
        "ha_heartbeat",
        "healthchecks_drift",
        "host_packages",
        "n8n",
        "prowlarr_indexers",
        "r2_usage",
        "scrutiny",
        "speedtest",
        "uptime_robot_monitors",
    }
)
QUERIED = re.compile(
    r"homelab_check_detail\{job='monitor-bridge',check='([a-z0-9_]+)'\}"
)
ROW = re.compile(r'check="([a-z0-9_]+)"')


def _row_rules() -> dict[str, str]:
    """`{check: summary}` for every rule that selects one row's homelab_check_success."""
    doc = yaml_fast.safe_load(RULES.read_text())
    found = {}
    for group in doc["groups"]:
        for rule in group["rules"]:
            expr = rule.get("expr", "")
            checks = set(ROW.findall(expr))
            if "homelab_check_success" in expr and len(checks) == 1:
                found[checks.pop()] = rule["annotations"]["summary"]
    return found


def test_every_detail_row_summary_queries_its_own_members_with_a_fallback():
    summaries = _row_rules()
    assert DETAIL_ROWS <= summaries.keys(), DETAIL_ROWS - summaries.keys()
    for check in sorted(DETAIL_ROWS):
        summary = summaries[check]
        assert QUERIED.findall(summary) == [check], (check, summary)
        assert "{{ else }}" in summary, (check, summary)
