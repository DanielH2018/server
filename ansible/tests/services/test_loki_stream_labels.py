"""loki-homelab's `job` label values have one owner: `loki_streams` in group_vars/all.yml (#3740).

Both Alloy configs stamp their streams from it, and monitor-bridge's env-secret renders its
selectors from it. Neither direction is checked by the render alone, so this holds both:

- every `job` value the owner lists is one an Alloy config emits, and every value an Alloy config
  emits is listed. A dead entry such as the `job="traefik"` monitor-bridge once selected on is
  the first half's failure.
- every `job` value a consumer selects on is one the owner lists. A selector on a value nothing
  emits matches no stream, and a freshness or error arm reads that silence as health.

Consumers that cannot render an Ansible var are read where they live: the Grafana dashboards'
queries against loki-homelab, the deploy-annotation expr, and every module-level `*_LOGQL`
constant in monitor-bridge and probe.py. The observability Loki is a separate store (decision
KL1) with its own labels, so dashboard queries against it are out of scope.
"""

import ast
import json
import re

import pytest

from _compose_render import rendered_text
from _helpers import ALL_VARS, load_yaml
from _k8s_render import render_role_template
from lib import yaml_fast
from lib.repo_paths import REPO

OWNER = load_yaml(ALL_VARS)["loki_streams"]
OWNER_JOBS = {labels["job"] for labels in OWNER.values()}

_OBSERVABILITY = REPO / "ansible/roles/k8s/observability"
_LOGQL_MODULE_ROOTS = (
    REPO / "ansible/roles/k8s/monitor-bridge/files",
    REPO / "scripts/diagnostics/probe_lib",
)


def _emitted_jobs(alloy_config: str) -> set[str]:
    """The `job` values a rendered Alloy config stamps, from file targets and relabel rules."""
    found = set(re.findall(r'"job"\s*=\s*"([^"]+)"', alloy_config))
    found |= set(
        re.findall(
            r'target_label\s*=\s*"job"\s*\n\s*replacement\s*=\s*"([^"]+)"', alloy_config
        )
    )
    return found


def _selected_jobs(expr: str) -> set[str]:
    """The `job` values a LogQL expression selects on with `=` or `=~`.

    A regex alternation is split into its members. A Grafana template variable (`$app`) is
    skipped, since its value is chosen at view time. A negative matcher selects nothing.
    """
    jobs = set()
    for op, value in re.findall(r'\bjob\s*(=~|=)\s*"([^"]*)"', expr):
        members = value.split("|") if op == "=~" else [value]
        jobs |= {m for m in members if m and "$" not in m}
    return jobs


def _alloy_configs() -> dict[str, str]:
    return {
        "loki-homelab": render_role_template("loki-homelab", "config/config.alloy.j2"),
        "alloy (daniel-pi)": rendered_text("alloy", "config.alloy.j2"),
    }


def _monitor_bridge_selectors() -> dict[str, str]:
    doc = yaml_fast.safe_load(
        render_role_template("monitor-bridge", "env-secret.yaml.j2")
    )
    return {
        "env-secret " + key: str(value)
        for key, value in doc["stringData"].items()
        if str(value).startswith("{") and "}" in str(value)
    }


def _logql_constants() -> dict[str, str]:
    """Every module-level `*_LOGQL` string constant under the monitor-bridge and probe roots."""
    found = {}
    for root in _LOGQL_MODULE_ROOTS:
        for path in sorted(root.rglob("*.py")):
            for node in ast.parse(path.read_text()).body:
                if (
                    isinstance(node, ast.Assign)
                    and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id.endswith("_LOGQL")
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                ):
                    found[f"{path.name} {node.targets[0].id}"] = node.value.value
    return found


def _dashboard_queries() -> dict[str, str]:
    """Each dashboard query whose datasource is loki-homelab, keyed by board and position."""
    uid = load_yaml(_OBSERVABILITY / "defaults/main.yml")[
        "observability_loki_homelab_uid"
    ]
    found = {}

    def walk(node, datasource, where):
        if isinstance(node, dict):
            datasource = node.get("datasource", datasource)
            for key, value in node.items():
                if key in ("expr", "query") and isinstance(value, str):
                    if isinstance(datasource, dict) and datasource.get("uid") == uid:
                        found[f"{where} #{len(found)}"] = value
                else:
                    walk(value, datasource, where)
        elif isinstance(node, list):
            for item in node:
                walk(item, datasource, where)

    for path in sorted((_OBSERVABILITY / "files/dashboards").rglob("*.json")):
        walk(json.loads(path.read_text()), None, path.name)
    return found


def _consumers() -> dict[str, str]:
    expr = load_yaml(_OBSERVABILITY / "defaults/main.yml")[
        "observability_deploy_annotation_expr"
    ]
    return {
        **_monitor_bridge_selectors(),
        **_logql_constants(),
        **_dashboard_queries(),
        "observability_deploy_annotation_expr": expr,
    }


def _unknown_jobs(consumers: dict[str, str]) -> dict[str, list[str]]:
    return {
        name: sorted(_selected_jobs(expr) - OWNER_JOBS)
        for name, expr in consumers.items()
        if _selected_jobs(expr) - OWNER_JOBS
    }


def test_the_owner_lists_every_stream_role():
    assert set(OWNER) >= {
        "cluster_pods",
        "host_syslog",
        "host_authlog",
        "k8s_audit",
        "pi_containers",
        "pi_health",
    }


@pytest.mark.parametrize("name", ["loki-homelab", "alloy (daniel-pi)"])
def test_each_alloy_config_emits_only_owner_values(name):
    emitted = _emitted_jobs(_alloy_configs()[name])
    assert emitted, f"{name}: no `job` value parsed, so the config shape changed"
    assert emitted <= OWNER_JOBS, (
        f"{name} stamps job values loki_streams does not list: {sorted(emitted - OWNER_JOBS)}"
    )


def test_every_owner_value_is_emitted():
    emitted = set().union(*(_emitted_jobs(text) for text in _alloy_configs().values()))
    assert OWNER_JOBS <= emitted, (
        "loki_streams lists job values no Alloy config emits, so a selector on them matches "
        f"nothing: {sorted(OWNER_JOBS - emitted)}"
    )


def test_the_consumer_census_finds_its_named_members():
    consumers = _consumers()
    for name in (
        "env-secret LOKI_STREAM",
        "env-secret LOG_ERROR_SELECTOR",
        "logs.py SWALLOWED_VERDICTS_LOGQL",
        "alerts.py SYSLOG_ALERT_LOGQL",
        "observability_deploy_annotation_expr",
    ):
        assert name in consumers, f"{name} is no longer found, so it is unchecked"
    boards = {name.split(" #")[0] for name in _dashboard_queries()}
    assert "alert-history.json" in boards, sorted(boards)


def test_every_consumer_selects_only_owner_values():
    unknown = _unknown_jobs(_consumers())
    assert not unknown, (
        "these consumers select job values no Alloy config emits, so the query matches no "
        f"stream and reads as silence: {unknown}"
    )


def test_a_consumer_on_a_dead_job_is_flagged():
    assert _unknown_jobs({"dead": '{job=~"authlog|syslog|traefik"}'}) == {
        "dead": ["traefik"]
    }


def test_a_consumer_on_owner_values_is_clean():
    assert not _unknown_jobs(
        {"ok": '{job=~"authlog|syslog", machine!="daniel-pi"}', "var": '{job="$app"}'}
    )


def test_a_config_emitting_an_unlisted_job_is_flagged():
    config = 'rule {\n    target_label = "job"\n    replacement  = "docker"\n  }'
    assert _emitted_jobs(config) - OWNER_JOBS == {"docker"}
