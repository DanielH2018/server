"""`scripts/validate/alert_conventions.py`: the rule conventions promtool does not check.

Most flagged cases copy the real alerting-meta rule file and its test into a temporary tree,
then edit one thing, so each sits one change away from the clean copy.

Run: uv run pytest scripts/validate/tests/test_validate_alert_conventions.py
"""

import pytest
import yaml

from lib import yaml_fast

from validate import alert_conventions


def _rule_tree(tmp_path, edit_rules=None, edit_tests=None):
    """The real alerting-meta rule file and its test, copied under `tmp_path`, each passed
    through an optional edit of its parsed form first.

    Returns:
        The rule directory and the test directory.
    """
    rules_dir = tmp_path / "files" / "rules"
    tests_dir = tmp_path / "tests" / "rules"
    rules_dir.mkdir(parents=True)
    tests_dir.mkdir(parents=True)
    rules = yaml_fast.safe_load(
        (alert_conventions.RULES_DIR / "alerting-meta.yaml").read_text()
    )
    tests = yaml_fast.safe_load(
        (alert_conventions.RULE_TESTS_DIR / "alerting-meta.test.yaml").read_text()
    )
    if edit_rules:
        edit_rules(rules)
    if edit_tests:
        edit_tests(tests)
    (rules_dir / "alerting-meta.yaml").write_text(yaml.safe_dump(rules))
    (tests_dir / "alerting-meta.test.yaml").write_text(yaml.safe_dump(tests))
    return rules_dir, tests_dir


def _meta_rules(doc):
    return doc["groups"][0]["rules"]


def _reload_rule(doc):
    """The PrometheusRuleReloadFailed rule, the one ordinary alert the flagged cases edit."""
    (rule,) = [
        r for r in _meta_rules(doc) if r.get("alert") == "PrometheusRuleReloadFailed"
    ]
    return rule


def _rule_problems(rules_dir, tests_dir, groups=("alerting-meta",)):
    return alert_conventions.rule_problems(
        list(groups),
        {f"{g}.yaml" for g in groups},
        rules_dir,
        tests_dir,
        templates_file=rules_dir / "absent.tmpl",
    )


def test_a_copy_of_the_tree_rules_is_clean(tmp_path):
    """The clean half every flagged case below edits one thing away from."""
    assert _rule_problems(*_rule_tree(tmp_path)) == []


def test_an_empty_rule_directory_is_flagged(tmp_path):
    rules_dir = tmp_path / "rules"
    tests_dir = tmp_path / "tests"
    rules_dir.mkdir()
    tests_dir.mkdir()
    problems = alert_conventions.rule_problems([], set(), rules_dir, tests_dir)
    assert any("no files/rules/alerting-meta.yaml" in p for p in problems), problems


def test_a_rule_file_the_list_does_not_name_is_flagged(tmp_path):
    rules_dir, tests_dir = _rule_tree(tmp_path)
    problems = _rule_problems(rules_dir, tests_dir, groups=())
    assert any("observability_rule_groups does not name it" in p for p in problems), (
        problems
    )


def test_a_listed_group_with_no_file_is_flagged(tmp_path):
    rules_dir, tests_dir = _rule_tree(tmp_path)
    problems = _rule_problems(rules_dir, tests_dir, groups=("alerting-meta", "flux"))
    assert any("names flux" in p for p in problems), problems


def test_a_rendered_configmap_missing_a_listed_group_is_flagged(tmp_path):
    rules_dir, tests_dir = _rule_tree(tmp_path)
    problems = alert_conventions.rule_problems(
        ["alerting-meta"], set(), rules_dir, tests_dir, rules_dir / "absent.tmpl"
    )
    assert any(
        "ConfigMap and observability_rule_groups disagree" in p for p in problems
    )


def test_an_alert_with_no_test_is_flagged(tmp_path):
    def drop_reload_cases(tests):
        for case in tests["tests"]:
            case["alert_rule_test"] = [
                c
                for c in case["alert_rule_test"]
                if c["alertname"] != "PrometheusRuleReloadFailed"
            ]

    problems = _rule_problems(*_rule_tree(tmp_path, edit_tests=drop_reload_cases))
    assert problems == [
        "alerting-meta/PrometheusRuleReloadFailed: no test case fires it"
    ]


def test_a_firing_case_that_asserts_no_severity_is_flagged(tmp_path):
    def drop_severity(tests):
        for case in tests["tests"]:
            for check in case["alert_rule_test"]:
                for alert in check.get("exp_alerts") or []:
                    alert["exp_labels"].pop("severity")

    problems = _rule_problems(*_rule_tree(tmp_path, edit_tests=drop_severity))
    assert len(problems) == 5, problems
    assert all("asserts both its service and severity" in p for p in problems), problems


@pytest.mark.parametrize(
    "labels,expected",
    [
        ({"severity": "page", "service": "observability"}, "severity is 'page'"),
        ({"service": "observability"}, "severity is None"),
        ({"severity": "warning"}, "carries no service label"),
        (
            {"severity": "warning", "service": "observability", "shadow": "yes"},
            "shadow is 'yes'",
        ),
    ],
)
def test_an_alert_with_a_label_outside_the_conventions_is_flagged(
    tmp_path, labels, expected
):
    def relabel(rules):
        _reload_rule(rules)["labels"] = labels

    problems = _rule_problems(*_rule_tree(tmp_path, edit_rules=relabel))
    assert any(expected in p for p in problems), problems


def test_a_shadow_alert_is_clean(tmp_path):
    def shadow(rules):
        _reload_rule(rules)["labels"]["shadow"] = "true"

    assert _rule_problems(*_rule_tree(tmp_path, edit_rules=shadow)) == []


# M2's join shape, as k8s-workloads.yaml writes it.
_MAPPED_EXPR = """
(
    (kube_deployment_status_replicas_unavailable > 0)
  * on(namespace, deployment) group_left(service)
    homelab:deployment_service:info
)
or
label_replace(
    (kube_deployment_status_replicas_unavailable > 0)
  unless on(namespace, deployment)
    homelab:deployment_service:info,
  "service", "unmapped", "", ""
)
"""


def _take_service_from(expr):
    def edit(rules):
        rule = _reload_rule(rules)
        rule["expr"] = expr
        del rule["labels"]["service"]

    return edit


def test_an_alert_taking_service_from_the_mapping_join_is_clean(tmp_path):
    edit = _take_service_from(_MAPPED_EXPR)
    assert _rule_problems(*_rule_tree(tmp_path, edit_rules=edit)) == []


def test_a_mapping_join_with_no_unmapped_fallback_is_flagged(tmp_path):
    join_only = _MAPPED_EXPR.split("\nor\n")[0]
    edit = _take_service_from(join_only)
    problems = _rule_problems(*_rule_tree(tmp_path, edit_rules=edit))
    assert any('no service="unmapped" fallback' in p for p in problems), problems


def test_a_group_not_named_like_its_file_is_flagged(tmp_path):
    def rename(rules):
        rules["groups"][0]["name"] = "meta"

    problems = _rule_problems(*_rule_tree(tmp_path, edit_rules=rename))
    assert any("not one group named alerting-meta" in p for p in problems), problems


def test_a_placeholder_in_a_rule_file_is_flagged(tmp_path):
    def substitute(rules):
        _reload_rule(rules)["annotations"]["summary"] = "Reload failed on ${host}."

    problems = _rule_problems(*_rule_tree(tmp_path, edit_rules=substitute))
    assert any("files/rules/alerting-meta.yaml: holds `${`" in p for p in problems), (
        problems
    )


def test_the_alertmanager_template_source_is_checked_for_placeholders(tmp_path):
    rules_dir, tests_dir = _rule_tree(tmp_path)
    templates = tmp_path / "files" / "alertmanager" / "homelab.tmpl"
    templates.parent.mkdir(parents=True)
    templates.write_text('{{ define "homelab.title" }}${title}{{ end }}\n')
    problems = alert_conventions.rule_problems(
        ["alerting-meta"], {"alerting-meta.yaml"}, rules_dir, tests_dir, templates
    )
    assert problems == [
        "files/alertmanager/homelab.tmpl: holds `${`. PromQL, promtool tests and Go "
        "templates have no such syntax, and Flux's substitution would rewrite it."
    ]


def test_a_test_file_with_no_zero_series_case_is_flagged(tmp_path):
    def feed_every_case(tests):
        for case in tests["tests"]:
            if not case["input_series"]:
                case["input_series"] = [{"series": "up", "values": "1"}]

    problems = _rule_problems(*_rule_tree(tmp_path, edit_tests=feed_every_case))
    assert any("no case with empty input_series" in p for p in problems), problems


def test_a_zero_series_case_that_fires_only_the_watchdog_is_flagged(tmp_path):
    """The Watchdog fires on any input, so an empty-input case firing it proves nothing."""

    def zero_series_fires_the_watchdog_alone(tests):
        for case in tests["tests"]:
            if not case["input_series"]:
                case["alert_rule_test"] = [
                    {
                        "eval_time": "1m",
                        "alertname": "Watchdog",
                        "exp_alerts": [
                            {
                                "exp_labels": {
                                    "severity": "info",
                                    "service": "observability",
                                }
                            }
                        ],
                    }
                ]
        tests["tests"].append(
            {
                "interval": "1m",
                "input_series": [{"series": "up", "values": "1"}],
                "alert_rule_test": [
                    {
                        "eval_time": "11m",
                        "alertname": "AlertmanagerMetricsMissing",
                        "exp_alerts": [
                            {
                                "exp_labels": {
                                    "severity": "warning",
                                    "service": "observability",
                                }
                            }
                        ],
                    }
                ],
            }
        )

    problems = _rule_problems(
        *_rule_tree(
            tmp_path,
            edit_tests=zero_series_fires_the_watchdog_alone,
        )
    )
    assert problems == [
        "tests/rules/alerting-meta.test.yaml: no case with empty input_series fires an alert "
        "other than the Watchdog. A family whose series vanish otherwise evaluates to nothing "
        "and stays quiet."
    ]


def test_a_rule_file_with_no_test_file_is_flagged(tmp_path):
    rules_dir, tests_dir = _rule_tree(tmp_path)
    (tests_dir / "alerting-meta.test.yaml").unlink()
    problems = _rule_problems(rules_dir, tests_dir)
    assert problems == [
        "files/rules/alerting-meta.yaml holds alerts and "
        "tests/rules/alerting-meta.test.yaml does not exist"
    ]


def _watchdog(group="alerting-meta", expr="vector(1)"):
    return {
        "alert": "Watchdog",
        "expr": expr,
        "labels": {"severity": "info", "service": "observability"},
    }


def test_one_watchdog_in_its_group_is_clean():
    docs = {
        "alerting-meta": {"groups": [{"name": "alerting-meta", "rules": [_watchdog()]}]}
    }
    assert alert_conventions.watchdog_problems(docs) == []


def test_no_watchdog_is_flagged():
    docs = {
        "alerting-meta": {
            "groups": [{"name": "alerting-meta", "rules": [{"alert": "Other"}]}]
        }
    }
    problems = alert_conventions.watchdog_problems(docs)
    assert len(problems) == 1 and "no rule is named Watchdog" in problems[0], problems


def test_a_second_watchdog_is_flagged():
    docs = {
        "alerting-meta": {
            "groups": [{"name": "alerting-meta", "rules": [_watchdog()]}]
        },
        "flux": {"groups": [{"name": "flux", "rules": [_watchdog()]}]},
    }
    problems = alert_conventions.watchdog_problems(docs)
    assert len(problems) == 1 and "2 rules are named Watchdog" in problems[0], problems


def test_a_watchdog_with_another_expression_is_flagged():
    docs = {
        "alerting-meta": {
            "groups": [{"name": "alerting-meta", "rules": [_watchdog(expr="up == 0")]}]
        }
    }
    problems = alert_conventions.watchdog_problems(docs)
    assert len(problems) == 1 and "'vector(1)'" in problems[0], problems


def test_an_always_on_alert_is_flagged(tmp_path):
    def always_on(rules):
        _reload_rule(rules).update(alert="AlwaysOn", expr="vector(1)")

    def rename_cases(tests):
        for case in tests["tests"]:
            for check in case["alert_rule_test"]:
                if check["alertname"] == "PrometheusRuleReloadFailed":
                    check["alertname"] = "AlwaysOn"

    problems = _rule_problems(
        *_rule_tree(tmp_path, edit_rules=always_on, edit_tests=rename_cases)
    )
    assert problems == [
        "alerting-meta/AlwaysOn: the expression 'vector(1)' names no metric, so the alert "
        "fires forever. Only the one Watchdog may do that."
    ]


@pytest.mark.parametrize(
    "expr",
    [
        'absent(up{job="alertmanager"})',
        "sum by (integration) (increase(alertmanager_notifications_failed_total[30m])) > 0",
        "up offset 5m == 0",
        "rate(foo_total[5m:1m]) > 1e3",
    ],
)
def test_an_expression_naming_a_metric_is_clean(expr):
    assert alert_conventions.names_a_metric(expr)


@pytest.mark.parametrize(
    "expr",
    [
        "vector(1)",
        "time() > 0",
        "scalar(vector(1)) and on() vector(0)",
        'label_replace(vector(1), "dst", "$1", "src", "(.*)")',
        "1e3 > 2",
    ],
)
def test_a_constant_expression_is_flagged(expr):
    assert not alert_conventions.names_a_metric(expr)
