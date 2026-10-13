"""`scripts/validate/alert_conventions.py`'s third `service` source: an alert takes `service`
from its scrape job's targets.

Each case copies the real alerting-meta rule file and its test, adds one alert with no literal
`service`, and checks it against a fixture `prometheus.yml`. The clean case is one change away
from each flagged one. promtool's half of condition 3 is the `--prove-red` fixture pair
`fixtures/alert_rules/*job-service-rules.test.yaml`, which CI runs with the real binary.

Run: uv run pytest scripts/validate/tests/test_validate_alert_service_from_job.py
"""

import pytest
import yaml

from lib import yaml_fast

from validate import alert_conventions


def _rule_tree(tmp_path, edit_rules, edit_tests):
    """The real alerting-meta rule file and its test under `tmp_path`, each edited first.

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
    edit_rules(rules)
    edit_tests(tests)
    (rules_dir / "alerting-meta.yaml").write_text(yaml.safe_dump(rules))
    (tests_dir / "alerting-meta.test.yaml").write_text(yaml.safe_dump(tests))
    return rules_dir, tests_dir


_PROBE_EXPR = 'probe_success{job="x"} == 0'
_LABELLED_TARGETS = [
    {"targets": ["https://a.example"], "labels": {"service": "a"}},
    {"targets": ["https://b.example"], "labels": {"service": "b"}},
]


def _prometheus_yml(job):
    """A rendered `prometheus.yml` holding job `x`, as `job` gives it, and job `y`."""
    return yaml.safe_dump(
        {
            "scrape_configs": [
                {"job_name": "x", **job},
                {"job_name": "y", "static_configs": _LABELLED_TARGETS},
            ]
        }
    )


def _from_job_problems(
    tmp_path, expr=_PROBE_EXPR, job=None, firing=({"service": "a"},)
):
    """The problems of the alerting-meta copy plus one alert with no literal `service`.

    Args:
        expr: The added alert's expression.
        job: Job `x`'s scrape config; every target sets `service` unless a case passes another.
        firing: The extra labels each firing case's one `exp_alerts` entry lists, beside
            `severity` and `job`.
    """

    def add_rule(rules):
        rules["groups"][0]["rules"].append(
            {
                "alert": "FixtureProbeFailed",
                "expr": expr,
                "labels": {"severity": "warning"},
            }
        )

    def add_cases(tests):
        tests["tests"].append(
            {
                "interval": "1m",
                "input_series": [
                    {"series": 'probe_success{job="x",service="a"}', "values": "0x5"}
                ],
                "alert_rule_test": [
                    {
                        "eval_time": "1m",
                        "alertname": "FixtureProbeFailed",
                        "exp_alerts": [
                            {
                                "exp_labels": {
                                    "severity": "warning",
                                    "job": "x",
                                    **labels,
                                }
                            }
                        ],
                    }
                    for labels in firing
                ],
            }
        )

    rules_dir, tests_dir = _rule_tree(
        tmp_path, edit_rules=add_rule, edit_tests=add_cases
    )
    return alert_conventions.rule_problems(
        ["alerting-meta"],
        {"alerting-meta.yaml"},
        rules_dir,
        tests_dir,
        templates_file=rules_dir / "absent.tmpl",
        prometheus_yml=_prometheus_yml(job or {"static_configs": _LABELLED_TARGETS}),
    )


def test_an_alert_taking_service_from_its_scrape_job_is_clean(tmp_path):
    assert alert_conventions.expression_jobs(_PROBE_EXPR) == [[("=", "x")]]
    assert _from_job_problems(tmp_path) == []


@pytest.mark.parametrize(
    "job,expected",
    [
        (
            {
                "static_configs": [
                    *_LABELLED_TARGETS,
                    {"targets": ["https://c.example"]},
                ]
            },
            "has a target whose labels set no service",
        ),
        (
            {
                "static_configs": [
                    {"targets": ["https://c.example"], "labels": {"service": ""}}
                ]
            },
            "has a target whose labels set no service",
        ),
        ({"kubernetes_sd_configs": [{"role": "pod"}]}, "through kubernetes_sd_configs"),
        ({"static_configs": []}, "has no static targets"),
        (
            {
                "static_configs": _LABELLED_TARGETS,
                "metric_relabel_configs": [{"action": "labeldrop", "regex": "serv.*"}],
            },
            "has a labeldrop rule whose regex 'serv.*' drops service or job in "
            "metric_relabel_configs",
        ),
        (
            {
                "static_configs": _LABELLED_TARGETS,
                "relabel_configs": [{"target_label": "service", "replacement": ""}],
            },
            "has a replace rule that writes service in relabel_configs",
        ),
        (
            {
                "static_configs": _LABELLED_TARGETS,
                "relabel_configs": [{"action": "labelkeep", "regex": "job|instance"}],
            },
            "has a labelkeep rule whose regex 'job|instance' drops service or job",
        ),
        (
            {
                "static_configs": _LABELLED_TARGETS,
                "relabel_configs": [{"action": "labelmap", "regex": "__meta_(.+)"}],
            },
            "has a labelmap rule",
        ),
    ],
)
def test_a_job_with_a_target_that_sets_no_service_is_flagged(tmp_path, job, expected):
    """Condition 2: every target of the job sets `service`."""
    problems = _from_job_problems(tmp_path, job=job)
    assert len(problems) == 1 and expected in problems[0], problems
    assert "carries no service label" in problems[0], problems


def test_a_blackbox_style_relabel_that_leaves_service_alone_is_clean(tmp_path):
    """The multi-target rewrite M4's job needs writes only `__param_*`, `instance` and
    `__address__`, and a labeldrop elsewhere leaves `service` and `job` in place."""
    job = {
        "static_configs": _LABELLED_TARGETS,
        "relabel_configs": [
            {"source_labels": ["__address__"], "target_label": "__param_target"},
            {"source_labels": ["__param_target"], "target_label": "instance"},
            {"target_label": "__address__", "replacement": "blackbox:9115"},
        ],
        "metric_relabel_configs": [{"action": "labeldrop", "regex": "pod|container"}],
    }
    assert _from_job_problems(tmp_path, job=job) == []


@pytest.mark.parametrize(
    "expr,expected",
    [
        ('probe_success{job=~"x"} == 0', "without exactly one job matcher by equality"),
        ("probe_success == 0", "without exactly one job matcher by equality"),
        (
            '{__name__="probe_success"} == 0',
            "without exactly one job matcher by equality",
        ),
        ('probe_success{job="x"} == 0 and up{job="y"} == 1', "from 2 jobs ['x', 'y']"),
        ('probe_success{job="z"} == 0', "which prometheus.yml does not scrape"),
        ("vector(1) > 0 and time() > 0", "has no vector selector"),
    ],
)
def test_an_alert_whose_selectors_do_not_name_one_job_is_flagged(
    tmp_path, expr, expected
):
    """Condition 1: every selector names the same job by equality."""
    problems = _from_job_problems(tmp_path, expr=expr)
    assert any(expected in p for p in problems), problems


def test_a_job_named_only_in_a_grouping_clause_is_flagged(tmp_path):
    """`on(job)` names a label, not a matcher, so the right-hand selector names no job."""
    expr = 'probe_success{job="x"} == 0 and on(job) up == 1'
    problems = _from_job_problems(tmp_path, expr=expr)
    assert any("without exactly one job matcher" in p for p in problems), problems


def test_an_aggregation_whose_firing_case_omits_service_is_flagged(tmp_path):
    """Condition 3. One firing case lists `service`, so M1's check that some case asserts it
    passes, and only the third source's every-entry rule flags the other."""
    problems = _from_job_problems(
        tmp_path,
        expr='max(probe_success{job="x"}) == 0',
        firing=({"service": "a"}, {}),
    )
    assert problems == [
        "alerting-meta/FixtureProbeFailed: takes service from its scrape job, and a firing "
        "case omits service from exp_labels. Only that assertion catches an expression that "
        "drops the label."
    ]
