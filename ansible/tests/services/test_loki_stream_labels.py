"""loki-homelab's `job` label values have one owner: `loki_streams` in group_vars/all.yml (#3740).

Both Alloy configs stamp their streams from it, and monitor-bridge's env-secret renders its
selectors from it. Neither direction is checked by the render alone, so this holds both:

- every `job` value the owner lists is one an Alloy config emits, and every value an Alloy config
  emits is listed. A dead entry such as the `job="traefik"` monitor-bridge once selected on is
  the first half's failure.
- every `job` value a consumer selects on is one the owner lists. A selector on a value nothing
  emits matches no stream, and a freshness or error arm reads that silence as health.

Consumers that cannot render an Ansible var are read where they live: the Grafana dashboards'
queries against loki-homelab, the deploy-annotation expr, every module-level `*_LOGQL`
constant in monitor-bridge and probe.py, and every rule in loki-homelab's `files/rules/`. The
observability Loki is a separate store (decision KL1) with its own labels, so dashboard queries
against it are out of scope.

The rule files get four more checks, because a `job` check passes a `container`, `machine` or
`app` matcher unread, and a rule on a selector that matches nothing never fires: each label name
is one the Alloy streams carry, each `machine` value is one `loki_streams` lists, each
`container` value is a container a rendered template declares, and each silence rule stays on
its side of the Pi boundary (#3739).
"""

import ast
import json
import re

import pytest

from _compose_render import rendered_text
from _helpers import ALL_VARS, load_yaml
from _k8s_render import render_role_template
from _loki_streams import (
    LOKI_STREAM_LABELS,
    _PI_STREAMS,
    _selector_labels,
    _selector_matches,
)
from lib import yaml_fast
from lib.repo_paths import REPO

OWNER = load_yaml(ALL_VARS)["loki_streams"]
OWNER_JOBS = {labels["job"] for labels in OWNER.values()}

OWNER_MACHINES = {labels["machine"] for labels in OWNER.values() if "machine" in labels}

_OBSERVABILITY = REPO / "ansible/roles/k8s/observability"
_LOKI_RULES = REPO / "ansible/roles/k8s/loki-homelab/files/rules"
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


def _loki_rule_exprs() -> dict[str, str]:
    """Each rule's `expr` in loki-homelab's rule files, keyed `<file> <rule name>`."""
    found = {}
    for path in sorted(_LOKI_RULES.glob("*.yaml")):
        for group in load_yaml(path)["groups"]:
            for rule in group["rules"]:
                name = rule.get("alert") or rule.get("record")
                found[f"{path.name} {name}"] = rule["expr"]
    return found


def _consumers() -> dict[str, str]:
    expr = load_yaml(_OBSERVABILITY / "defaults/main.yml")[
        "observability_deploy_annotation_expr"
    ]
    return {
        **_monitor_bridge_selectors(),
        **_logql_constants(),
        **_dashboard_queries(),
        **_loki_rule_exprs(),
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


def _unemitted(owner_jobs: set[str], configs: dict[str, str]) -> set[str]:
    emitted = set().union(*(_emitted_jobs(text) for text in configs.values()))
    return owner_jobs - emitted


def test_every_owner_value_is_emitted():
    dead = _unemitted(OWNER_JOBS, _alloy_configs())
    assert not dead, (
        "loki_streams lists job values no Alloy config emits, so a selector on them matches "
        f"nothing: {sorted(dead)}"
    )


def test_an_owner_value_nothing_emits_is_flagged():
    assert _unemitted(OWNER_JOBS | {"traefik"}, _alloy_configs()) == {"traefik"}


def test_the_consumer_census_finds_its_named_members():
    consumers = _consumers()
    for name in (
        "env-secret LOKI_STREAM",
        "env-secret LOG_ERROR_SELECTOR",
        "logs.py SWALLOWED_VERDICTS_LOGQL",
        "alerts.py SYSLOG_ALERT_LOGQL",
        "observability_deploy_annotation_expr",
        "homelab-logs.yaml LokiPodLogsSilent",
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


# The rule-file checks. A selector reaches them through `_stream_selectors`, so a rule with two
# selectors has both read.


def _stream_selectors(expr: str) -> list[str]:
    """Each `{...}` stream selector in a LogQL expression."""
    return re.findall(r"\{[^{}]*\}", expr)


def _label_values(expr: str, label: str) -> set[str]:
    """The values an expression's selectors name for `label`, with any matcher.

    A regex alternation is split into its members, so `=~"a|b"` names `a` and `b`, and a
    pattern such as `.+` stays one value no owner lists.
    """
    values = set()
    for selector in _stream_selectors(expr):
        for name, op, value in re.findall(
            r'(\w+)\s*(=~|!~|!=|=)\s*"([^"]*)"', selector
        ):
            if name == label:
                values |= set(value.split("|")) if "~" in op else {value}
    return values


def _unknown_labels(exprs: dict[str, str]) -> dict[str, list[str]]:
    found = {}
    for name, expr in exprs.items():
        labels = set().union(*(_selector_labels(s) for s in _stream_selectors(expr)))
        if labels - LOKI_STREAM_LABELS:
            found[name] = sorted(labels - LOKI_STREAM_LABELS)
    return found


def test_loki_rules_select_on_stream_labels_is_clean():
    assert not _unknown_labels(_loki_rule_exprs()), (
        "a Loki rule selects on a label the Alloy streams do not carry, so it matches no "
        "stream and its count reads zero forever"
    )


def test_a_loki_rule_on_an_app_label_is_flagged():
    assert _unknown_labels({"r": 'count_over_time({app="uptime-kuma"}[3h])'}) == {
        "r": ["app"]
    }


def _unknown_machines(exprs: dict[str, str]) -> dict[str, list[str]]:
    return {
        name: sorted(_label_values(expr, "machine") - OWNER_MACHINES)
        for name, expr in exprs.items()
        if _label_values(expr, "machine") - OWNER_MACHINES
    }


def test_loki_rules_name_owner_machines_is_clean():
    exprs = _loki_rule_exprs()
    named = set().union(*(_label_values(e, "machine") for e in exprs.values()))
    assert "daniel-pi" in named, "no rule names a machine, so this check reads nothing"
    assert not _unknown_machines(exprs), (
        "a Loki rule names a machine no loki_streams stream carries; a `!=` on it excludes "
        "nothing and an `=` on it matches nothing"
    )


def test_a_loki_rule_on_an_unknown_machine_is_flagged():
    expr = 'absent_over_time({job="syslog", machine!="daniel-laptop"}[3h])'
    assert _unknown_machines({"r": expr}) == {"r": ["daniel-laptop"]}


# Each `container` value a Loki rule selects on, and the role template whose pod declares a
# container by that name. A value with no entry here fails, so a new rule adds its mapping.
_RULE_CONTAINERS = {"uptime-kuma": ("uptime-kuma", "deployment.yaml.j2")}


def _declared_containers(role: str, template: str) -> set[str]:
    names = set()
    for doc in yaml_fast.safe_load_all(render_role_template(role, template)):
        pod = ((doc or {}).get("spec") or {}).get("template", {}).get("spec") or {}
        names |= {c["name"] for c in pod.get("containers", [])}
    return names


def _undeclared_containers(
    exprs: dict[str, str], mapping: dict[str, tuple[str, str]] = _RULE_CONTAINERS
) -> dict[str, list[str]]:
    found = {}
    for name, expr in exprs.items():
        for value in sorted(_label_values(expr, "container")):
            if value not in mapping:
                found.setdefault(name, []).append(f"{value}: no template is mapped")
            elif value not in _declared_containers(*mapping[value]):
                role, template = mapping[value]
                found.setdefault(name, []).append(
                    f"{value}: {role}/{template} declares none"
                )
    return found


def test_loki_rules_select_declared_containers_is_clean():
    exprs = _loki_rule_exprs()
    named = set().union(*(_label_values(e, "container") for e in exprs.values()))
    assert "uptime-kuma" in named, (
        "no rule names a container, so this check reads nothing"
    )
    assert not _undeclared_containers(exprs)


def test_a_loki_rule_on_an_undeclared_container_is_flagged():
    expr = 'count_over_time({container="uptime-kuma2"} |= "x" [3h])'
    assert _undeclared_containers({"r": expr}) == {
        "r": ["uptime-kuma2: no template is mapped"]
    }
    mapping = {"uptime-kuma2": ("uptime-kuma", "deployment.yaml.j2")}
    assert _undeclared_containers({"r": expr}, mapping) == {
        "r": ["uptime-kuma2: uptime-kuma/deployment.yaml.j2 declares none"]
    }


# Each silence rule, and the `loki_streams` roles whose silence it reports. A cluster rule that
# also matched a Pi stream would keep its count above zero through a total cluster outage and
# never fire, and the Pi rule must not be held up by the cluster either (#3739).
_SILENCE_RULES = {
    "homelab-logs.yaml LokiHostLogsSilent": {"host_syslog", "host_authlog"},
    "homelab-logs.yaml LokiPodLogsSilent": {"cluster_pods"},
    "homelab-logs.yaml LokiPiLogsSilent": {"pi_containers"},
}


def _stream_labels(role: str) -> dict[str, str]:
    """A stream's labels: the owner's, plus the `machine` and `container` a live stream adds."""
    if role in _PI_STREAMS:
        return _PI_STREAMS[role]
    return {**OWNER[role], "machine": "daniel-box", "container": "authelia"}


def _pi_boundary_problems(
    exprs: dict[str, str], rules: dict[str, set[str]] = _SILENCE_RULES
) -> list[str]:
    problems = []
    for name, own in rules.items():
        selectors = _stream_selectors(exprs.get(name, ""))
        if len(selectors) != 1:
            problems.append(f"{name}: {len(selectors)} stream selectors, not one")
            continue
        on_pi = own <= set(_PI_STREAMS)
        for role in OWNER:
            matches = _selector_matches(selectors[0], _stream_labels(role))
            if role in own and not matches:
                problems.append(f"{name}: misses its own {role} stream")
            elif matches and (role in _PI_STREAMS) != on_pi:
                problems.append(f"{name}: also matches {role}, across the Pi boundary")
    return problems


def test_the_silence_rules_are_the_absent_rules():
    absent = {n for n, e in _loki_rule_exprs().items() if "absent_over_time" in e}
    assert absent == set(_SILENCE_RULES), (
        "every silence rule needs its streams in _SILENCE_RULES, or the Pi boundary goes "
        f"unchecked for it: {sorted(absent ^ set(_SILENCE_RULES))}"
    )


def test_the_silence_rules_hold_the_pi_boundary_is_clean():
    assert not _pi_boundary_problems(_loki_rule_exprs())


def test_a_cluster_silence_rule_that_counts_a_pi_stream_is_flagged():
    # The host selector as monitor-bridge deployed it before #3739.
    expr = 'absent_over_time({job=~"authlog|syslog"}[3h])'
    rules = {"r": {"host_syslog", "host_authlog"}}
    assert _pi_boundary_problems({"r": expr}, rules) == [
        "r: also matches pi_health, across the Pi boundary"
    ]
