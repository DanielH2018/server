"""PromotionStalled stays quiet while the weekly reboot's no-promotion window defers (#5458).

The exporter serves `homelab_deploy_reboot_window_active` from the `weekly_reboot_*` schedule,
so the rule file stays plain YAML with no literal hour. Its `and on()` drops the alert when the
gauge is missing as well as when it reads 1, so DeployPipelineExporterAbsent must page a
missing gauge, as it does for the hold gauge.

Run: uv run pytest ansible/tests/services/test_deploy_pipeline_reboot_window_rule.py
"""

import re

from lib import yaml_fast
from lib.repo_paths import REPO

RULES = REPO / "ansible/roles/k8s/observability/files/rules/deploy-pipeline.yaml"
GAUGE = "homelab_deploy_reboot_window_active"


def expr(alert: str) -> str:
    groups = yaml_fast.safe_load(RULES.read_text(encoding="utf-8"))["groups"]
    [rule] = [r for g in groups for r in g["rules"] if r.get("alert") == alert]
    return rule["expr"]


def test_promotion_stalled_is_conjoined_with_the_window_gauge_at_zero():
    found = expr("PromotionStalled")
    assert re.search(
        r"and\s+on\s*\(\s*\)\s*\(\s*%s(\{[^}]*\})?\s*==\s*0\s*\)" % GAUGE, found
    ), found


def test_the_absent_alert_pages_a_missing_window_gauge():
    found = expr("DeployPipelineExporterAbsent")
    assert re.search(r"absent\(\s*%s(\{[^}]*\})?\s*\)" % GAUGE, found), found
