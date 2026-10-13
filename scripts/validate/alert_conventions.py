"""Hold the observability role's Prometheus rule files to the conventions promtool cannot check.

promtool accepts a rule that pages nobody useful, and a rule file that never reaches
Prometheus. `rule_problems` holds the role's rule files to the conventions in
`docs/target-architecture/monitoring/m1-alertmanager.md`:

- every rule file parses, and holds one group named like the file;
- the stems in `files/rules/`, `observability_rule_groups` and the rendered `prometheus-rules`
  ConfigMap's keys are one set, because a file the list does not name never reaches the
  ConfigMap. `GROUP_FLOOR` names the groups the run must find;
- every alert carries a literal `severity` from `SEVERITIES`, and `shadow` is absent or
  `"true"`;
- every alert carries a `service` from one of three sources:
    - a literal label;
    - M2's join shape (`service_from_mapping`): a `group_left(service)` join to a
      workload-service recording rule, with a `label_replace` that maps a workload the join
      misses to `service="unmapped"`;
    - its scrape job's targets (`service_from_job`). Every vector selector in the expression
      names one job by equality, every target of that job in the rendered `prometheus.yml`
      sets `service` in its `labels` (`service_jobs`), and every `exp_alerts` entry for the
      alert lists `service` in `exp_labels`. promtool compares whole label sets, so the last
      condition fails an expression that aggregates `service` away;
- exactly one rule is named `Watchdog`, in `alerting-meta`, with the expression `vector(1)`.
  The C7 health gate excludes that name, so a second one would be hidden from every gate, and
  with none nothing pings the `alertmanager-watchdog` check on Healthchecks.io. Every other
  alert's expression names a metric (`names_a_metric`), because a constant expression fires
  forever;
- no rule, rule test or Alertmanager template source holds `${`. The rendered
  `alertmanager.yml` is outside this, because it carries placeholders by design;
- a test case fires every alert and asserts its `service` and `severity`;
- each test file whose rule file holds an alert has a case with no input series that fires an
  alert other than the Watchdog. A family whose series vanish otherwise evaluates to nothing
  and stays quiet, and the Watchdog fires on any input, so it proves nothing about the family.

`scripts/validate/alert_rules.py` runs `rule_problems` before its tool phase, with the rule
groups, the ConfigMap keys and `prometheus.yml` it reads from the role's render. This module
renders nothing.
"""

import re
import sys
from collections.abc import Mapping
from pathlib import Path

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml

from lib import yaml_fast
from lib.repo_paths import K8S_ROLES

ROLE_DIR = K8S_ROLES / "observability"
RULES_DIR = ROLE_DIR / "files" / "rules"
RULE_TESTS_DIR = ROLE_DIR / "tests" / "rules"

# The ConfigMap that carries the rule files, and the rule groups a run must find. The other
# member a run must find is the one `Watchdog`, which `watchdog_problems` requires.
RULES_CONFIGMAP = "prometheus-rules"
GROUP_FLOOR = frozenset({"alerting-meta"})
RULE_TEST_SUFFIX = ".test.yaml"

SEVERITIES = frozenset({"critical", "warning", "info"})

# M2's join shape, from docs/target-architecture/monitoring/m2-first-bridge-family.md, "The
# rules": the join that takes `service` from a workload-service recording rule, and the
# fallback that keeps an alert whose workload the join misses. A join with no fallback drops
# the alert whenever its mapping is missing.
SERVICE_JOIN = re.compile(
    r"group_left\s*\(\s*service\s*\)\s*homelab:(?:deployment|daemonset|pod)_service:info\b"
)
SERVICE_FALLBACK = re.compile(r'"service"\s*,\s*"unmapped"\s*,\s*""\s*,\s*""')
WATCHDOG = "Watchdog"
WATCHDOG_GROUP = "alerting-meta"
WATCHDOG_EXPR = "vector(1)"

# The source files that must hold no `${`: Flux's substitution would rewrite one, so the
# rendered ConfigMap would differ from the file promtool checked.
TEMPLATES_FILE = ROLE_DIR / "files" / "alertmanager" / "homelab.tmpl"
FLUX_PLACEHOLDER = "${"

# What `names_a_metric` strips before it looks for a metric name: string literals, label
# matchers, range and subquery selectors, and the label lists of grouping and matching clauses.
PROMQL_STRING = re.compile(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|`[^`]*`')
PROMQL_BRACES = re.compile(r"\{[^}]*\}")
PROMQL_RANGE = re.compile(r"\[[^\]]*\]")
PROMQL_LABEL_LIST = re.compile(
    r"\b(?:by|without|on|ignoring|group_left|group_right)\s*\([^)]*\)"
)
PROMQL_IDENTIFIER = re.compile(r"(?<![\w.:])([A-Za-z_:][\w:]*)(\s*\()?")
PROMQL_KEYWORDS = frozenset(
    {
        "and",
        "or",
        "unless",
        "bool",
        "offset",
        "by",
        "without",
        "on",
        "ignoring",
        "group_left",
        "group_right",
        "inf",
        "nan",
        "atan2",
    }
)


def names_a_metric(expr: str) -> bool:
    """Whether a PromQL expression names a metric, read lexically.

    An identifier that is not a keyword and is not followed by `(` is a metric name once the
    string literals, label matchers, ranges and label lists are gone. The read can miss a
    constant expression written in an unusual form; the Watchdog check does not depend on it,
    because that one matches the rule's name exactly.
    """
    text = PROMQL_STRING.sub(" ", expr)
    text = PROMQL_BRACES.sub(" ", text)
    text = PROMQL_RANGE.sub(" ", text)
    text = PROMQL_LABEL_LIST.sub(" ", text)
    return any(
        not call and name.lower() not in PROMQL_KEYWORDS
        for name, call in PROMQL_IDENTIFIER.findall(text)
    )


def service_from_mapping(expr: str) -> str | None:
    """Why an alert with no literal `service` fails to take one from its expression, or None.

    Read lexically, like `names_a_metric`: the expression must join a workload-service
    recording rule with `group_left(service)` and map what the join misses to
    `service="unmapped"`.
    """
    if not SERVICE_JOIN.search(expr):
        return "carries no service label, so no health gate reads it"
    if not SERVICE_FALLBACK.search(expr):
        return (
            'takes service from a workload-service join with no service="unmapped" '
            "fallback, so a missing mapping silences it"
        )
    return None


# What `expression_jobs` reads: the vector selectors of an expression, after its grouping and
# matching label lists are gone. A string literal is matched whole so its text never reads as
# a selector; a `{...}` block is kept, because it holds the `job` matcher.
PROMQL_SELECTOR = re.compile(
    r'(?P<string>"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|`[^`]*`)'
    r"|(?P<range>\[[^\]]*\])"
    r"|(?<![\w.:])(?P<name>[A-Za-z_:][\w:]*)\s*(?P<call>\()?(?P<matchers>\{[^}]*\})?"
    r"|(?P<bare>\{[^}]*\})"
)
JOB_MATCHER = re.compile(r'(?<![\w.])job\s*(=~|!~|!=|=)\s*"((?:[^"\\]|\\.)*)"')


def expression_jobs(expr: str) -> list[list[tuple[str, str]]]:
    """The `job` matchers of each vector selector in a PromQL expression, read lexically.

    Each item is one selector's `job` matchers as (operator, value) pairs; a selector with
    none gives an empty list. A function call, a keyword and a string literal are not
    selectors, and neither is a label inside `by`, `without`, `on` or `ignoring`.
    """
    selectors = []
    for match in PROMQL_SELECTOR.finditer(PROMQL_LABEL_LIST.sub(" ", expr)):
        if match["bare"]:
            selectors.append(JOB_MATCHER.findall(match["bare"]))
        elif match["name"] and not match["call"]:
            if match["name"].lower() in PROMQL_KEYWORDS:
                continue
            selectors.append(JOB_MATCHER.findall(match["matchers"] or ""))
    return selectors


# The labels the third `service` source reads from a job's series: `service` itself, and
# `job`, which the expression's matcher compares with the job's name.
JOB_SERVICE_LABELS = ("service", "job")
RELABEL_SECTIONS = ("relabel_configs", "metric_relabel_configs")


def relabel_problem(rule: object) -> str | None:
    """Why one relabel rule can drop or rewrite `service` or `job`, or None.

    Prometheus anchors a relabel regex, so `labeldrop` removes a label its regex matches whole
    and `labelkeep` removes one it does not. `labelmap` writes labels whose names come from
    the source label names, so it is refused whatever its regex. Any other action writes only
    `target_label`.
    """
    if not isinstance(rule, dict):
        return None
    action = str(rule.get("action", "replace")).lower()
    regex = str(rule.get("regex", "(.*)"))
    try:
        matches = {name: bool(re.fullmatch(regex, name)) for name in JOB_SERVICE_LABELS}
    except re.error:
        return f"has a {action} rule whose regex {regex!r} does not compile here"
    if action == "labelmap":
        return "has a labelmap rule, which can overwrite service or job"
    if action == "labeldrop" and any(matches.values()):
        return f"has a labeldrop rule whose regex {regex!r} drops service or job"
    if action == "labelkeep" and not all(matches.values()):
        return f"has a labelkeep rule whose regex {regex!r} drops service or job"
    if rule.get("target_label") in JOB_SERVICE_LABELS:
        return f"has a {action} rule that writes {rule['target_label']}"
    return None


def service_jobs(prometheus_yml: str | None) -> dict[str, str | None]:
    """Each scrape job in a rendered `prometheus.yml`, and why its targets fail to carry `service`.

    A job maps to None when it has static targets only and every `static_configs` item sets a
    non-empty `service` in its `labels`. A discovered target sets no static label, so a job
    with any other `*_configs` discovery fails, as does one with no targets. A job fails too
    when a rule in its `relabel_configs` or `metric_relabel_configs` can drop or rewrite
    `service` or `job` (`relabel_problem`): the static label would not reach the series, and
    promtool's synthetic input series in condition 3 would not show it.
    """
    try:
        config = yaml_fast.safe_load(prometheus_yml or "") or {}
    except yaml.YAMLError:
        return {}
    jobs = {}
    for job in config.get("scrape_configs") or [] if isinstance(config, dict) else []:
        if not isinstance(job, dict) or "job_name" not in job:
            continue
        discovered = sorted(
            key
            for key in job
            if key.endswith("_sd_configs") and key != "static_configs"
        )
        static = [
            item for item in job.get("static_configs") or [] if isinstance(item, dict)
        ]
        relabelled = next(
            (
                f"{why} in {section}"
                for section in RELABEL_SECTIONS
                for rule in job.get(section) or []
                if (why := relabel_problem(rule))
            ),
            None,
        )
        if discovered:
            jobs[job["job_name"]] = f"discovers its targets through {discovered[0]}"
        elif relabelled:
            jobs[job["job_name"]] = relabelled
        elif not static:
            jobs[job["job_name"]] = "has no static targets"
        elif not all((item.get("labels") or {}).get("service") for item in static):
            jobs[job["job_name"]] = "has a target whose labels set no service"
        else:
            jobs[job["job_name"]] = None
    return jobs


def service_from_job(
    expr: str, jobs: Mapping[str, str | None]
) -> tuple[str | None, str | None]:
    """The job an alert with no literal `service` takes it from, or None and why it cannot.

    Conditions 1 and 2 of the third `service` source: every vector selector names the same
    job by equality, and every target of that job sets `service`. Condition 3 reads the rule
    tests, so `rule_test_problems` checks it.

    Args:
        expr: The alert's expression.
        jobs: `service_jobs` of the rendered `prometheus.yml`.
    """
    selectors = expression_jobs(expr)
    if not selectors:
        return None, "has no vector selector to take service from"
    names = set()
    for matchers in selectors:
        if len(matchers) != 1 or matchers[0][0] != "=":
            return None, (
                "has a selector without exactly one job matcher by equality, so its series "
                "need not come from a job whose targets set service"
            )
        names.add(matchers[0][1])
    if len(names) > 1:
        return None, f"selects from {len(names)} jobs {sorted(names)}, not one"
    (job,) = names
    if job not in jobs:
        return None, f"selects job {job!r}, which prometheus.yml does not scrape"
    if jobs[job]:
        return None, f"selects job {job!r}, which {jobs[job]}"
    return job, None


def _load_yaml(path: Path) -> tuple[object, str | None]:
    """The parsed file, or None and the parse error."""
    try:
        return yaml_fast.safe_load(path.read_text()), None
    except yaml.YAMLError as err:
        return None, f"{path.name}: does not parse: {err}"


def _alerts(doc: object) -> list[tuple[str, dict]]:
    """Every alerting rule in a parsed rule file, as (group name, rule) pairs."""
    groups = doc.get("groups") if isinstance(doc, dict) else None
    return [
        (group.get("name"), rule)
        for group in groups or []
        if isinstance(group, dict)
        for rule in group.get("rules") or []
        if isinstance(rule, dict) and "alert" in rule
    ]


def group_list_problems(
    stems: set[str], groups: list[str], keys: set[str]
) -> list[str]:
    """A problem for each group the rule directory, the list and the ConfigMap disagree on.

    Args:
        stems: The rule files' names without `.yaml`.
        groups: `observability_rule_groups`.
        keys: The rendered `prometheus-rules` ConfigMap's data keys.
    """
    problems = [
        f"rule group {group}: no files/rules/{group}.yaml, so nothing checks the group "
        "the M1 slice plan requires"
        for group in sorted(GROUP_FLOOR - stems)
    ]
    rendered = {key.removesuffix(".yaml") for key in keys}
    for stem in sorted(stems - set(groups)):
        problems.append(
            f"files/rules/{stem}.yaml: observability_rule_groups does not name it, so it never "
            "reaches the prometheus-rules ConfigMap. Add the group to the list."
        )
    for group in sorted(set(groups) - stems):
        problems.append(
            f"observability_rule_groups names {group}, and files/rules/{group}.yaml does not "
            "exist"
        )
    for group in sorted((stems & set(groups)) ^ rendered):
        problems.append(
            f"rule group {group}: the rendered {RULES_CONFIGMAP} ConfigMap and "
            "observability_rule_groups disagree on it"
        )
    return problems


def rule_file_problems(
    stem: str, doc: object, jobs: Mapping[str, str | None] | None = None
) -> list[str]:
    """A problem for each convention one parsed rule file breaks on its own.

    Args:
        stem: The rule file's name without `.yaml`.
        doc: The parsed rule file.
        jobs: `service_jobs` of the rendered `prometheus.yml`; None reads as no job.
    """
    groups = doc.get("groups") if isinstance(doc, dict) else None
    names = [g.get("name") for g in groups or [] if isinstance(g, dict)]
    problems = []
    if names != [stem]:
        problems.append(
            f"files/rules/{stem}.yaml: holds the groups {names}, not one group named {stem}"
        )
    for _group, rule in _alerts(doc):
        name = rule["alert"]
        labels = rule.get("labels") or {}
        if labels.get("severity") not in SEVERITIES:
            problems.append(
                f"{stem}/{name}: severity is {labels.get('severity')!r}, not a literal one of "
                f"{sorted(SEVERITIES)}"
            )
        if not labels.get("service"):
            expr = str(rule.get("expr", ""))
            if SERVICE_JOIN.search(expr):
                reason = service_from_mapping(expr)
            else:
                _job, why = service_from_job(expr, jobs or {})
                reason = why and (
                    "carries no service label, takes none from a workload-service join, "
                    f"and cannot take it from its scrape job: the expression {why}"
                )
            if reason:
                problems.append(f"{stem}/{name}: {reason}")
        if "shadow" in labels and labels["shadow"] != "true":
            problems.append(
                f'{stem}/{name}: shadow is {labels["shadow"]!r}; it is absent or "true"'
            )
        if name != WATCHDOG and not names_a_metric(str(rule.get("expr", ""))):
            problems.append(
                f"{stem}/{name}: the expression {rule.get('expr')!r} names no metric, so the "
                f"alert fires forever. Only the one {WATCHDOG} may do that."
            )
    return problems


def watchdog_problems(docs: Mapping[str, object]) -> list[str]:
    """A problem unless exactly one rule is the Watchdog, in its group, as `vector(1)`.

    Args:
        docs: Each parsed rule file, by stem.
    """
    found = [
        (stem, group, rule)
        for stem, doc in docs.items()
        for group, rule in _alerts(doc)
        if rule["alert"] == WATCHDOG
    ]
    if not found:
        return [
            f"no rule is named {WATCHDOG}, so nothing pings the alertmanager-watchdog check on "
            f"Healthchecks.io. It belongs in the {WATCHDOG_GROUP} group as {WATCHDOG_EXPR!r}."
        ]
    if len(found) > 1:
        return [
            f"{len(found)} rules are named {WATCHDOG}, in {sorted(s for s, _, _ in found)}. "
            "The health gate excludes that name, so every one but the real Watchdog would be "
            "hidden from it."
        ]
    return [
        f"{stem}/{WATCHDOG}: must sit in the {WATCHDOG_GROUP} group with the expression "
        f"{WATCHDOG_EXPR!r}, not in {group} with {str(rule.get('expr')).strip()!r}"
        for stem, group, rule in found
        if group != WATCHDOG_GROUP or str(rule.get("expr")).strip() != WATCHDOG_EXPR
    ]


def placeholder_problems(paths: list[Path]) -> list[str]:
    """A problem for each source file that holds `${`."""
    return [
        f"{path.relative_to(path.parents[2])}: holds `{FLUX_PLACEHOLDER}`. PromQL, promtool "
        "tests and Go templates have no such syntax, and Flux's substitution would rewrite it."
        for path in paths
        if FLUX_PLACEHOLDER in path.read_text()
    ]


def rule_test_problems(
    stem: str, alerts: list[str], tests: object, from_job: frozenset[str] = frozenset()
) -> list[str]:
    """A problem for each alert no case fires, and for a family with no zero-series case.

    Args:
        stem: The rule file's name without `.yaml`.
        alerts: The alert names that rule file holds.
        tests: The parsed `<stem>.test.yaml`, or None when it is missing.
        from_job: The alerts that take `service` from their scrape job. Every `exp_alerts`
            entry for one of them must list `service`, not just one firing case.
    """
    if not alerts:
        return []
    if tests is None:
        return [
            f"files/rules/{stem}.yaml holds alerts and tests/rules/{stem}{RULE_TEST_SUFFIX} "
            "does not exist"
        ]
    cases = [
        case
        for case in (tests.get("tests") if isinstance(tests, dict) else None) or []
        if isinstance(case, dict)
    ]
    fired = set()
    asserted = set()
    unasserted_job_service = set()
    zero_series = False
    for case in cases:
        for check in case.get("alert_rule_test") or []:
            expected = check.get("exp_alerts") or []
            if not expected:
                continue
            fired.add(check.get("alertname"))
            if not all(
                "service" in (alert.get("exp_labels") or {}) for alert in expected
            ):
                unasserted_job_service.add(check.get("alertname"))
            if all(
                {"service", "severity"} <= set(alert.get("exp_labels") or {})
                for alert in expected
            ):
                asserted.add(check.get("alertname"))
            if not case.get("input_series") and check.get("alertname") != WATCHDOG:
                zero_series = True
    problems = [
        f"{stem}/{name}: no test case fires it" for name in alerts if name not in fired
    ]
    problems += [
        f"{stem}/{name}: no firing case asserts both its service and severity labels"
        for name in alerts
        if name in fired and name not in asserted
    ]
    problems += [
        f"{stem}/{name}: takes service from its scrape job, and a firing case omits service "
        "from exp_labels. Only that assertion catches an expression that drops the label."
        for name in alerts
        if name in from_job and name in unasserted_job_service
    ]
    if not zero_series:
        problems.append(
            f"tests/rules/{stem}{RULE_TEST_SUFFIX}: no case with empty input_series fires an "
            f"alert other than the {WATCHDOG}. A family whose series vanish otherwise "
            "evaluates to nothing and stays quiet."
        )
    return problems


def rule_problems(
    groups: list[str],
    keys: set[str],
    rules_dir: Path = RULES_DIR,
    tests_dir: Path = RULE_TESTS_DIR,
    templates_file: Path = TEMPLATES_FILE,
    prometheus_yml: str | None = None,
) -> list[str]:
    """A problem for each rule convention the role's rule files and their tests break.

    Args:
        groups: `observability_rule_groups`.
        keys: The rendered `prometheus-rules` ConfigMap's data keys.
        rules_dir: The rule files.
        tests_dir: Their promtool tests.
        templates_file: The Alertmanager template source, checked for `${` with the rules.
        prometheus_yml: The rendered `prometheus.yml`, whose scrape jobs the third `service`
            source reads. None leaves every job unknown, so no alert takes `service` from one.
    """
    rule_paths = sorted(rules_dir.glob("*.yaml"))
    test_paths = sorted(tests_dir.glob(f"*{RULE_TEST_SUFFIX}"))
    problems = placeholder_problems(
        [
            *rule_paths,
            *test_paths,
            *([templates_file] if templates_file.is_file() else []),
        ]
    )
    docs = {}
    for path in rule_paths:
        doc, err = _load_yaml(path)
        if err:
            problems.append(err)
        else:
            docs[path.stem] = doc
    problems += group_list_problems({p.stem for p in rule_paths}, groups, keys)
    problems += watchdog_problems(docs)
    tests = {}
    for path in test_paths:
        stem = path.name.removesuffix(RULE_TEST_SUFFIX)
        if stem not in {p.stem for p in rule_paths}:
            problems.append(
                f"tests/rules/{path.name}: no files/rules/{stem}.yaml to test"
            )
        doc, err = _load_yaml(path)
        if err:
            problems.append(err)
        else:
            tests[stem] = doc
    jobs = service_jobs(prometheus_yml)
    for stem, doc in docs.items():
        problems += rule_file_problems(stem, doc, jobs)
        alerts = [rule["alert"] for _group, rule in _alerts(doc)]
        from_job = frozenset(
            rule["alert"]
            for _group, rule in _alerts(doc)
            if not (rule.get("labels") or {}).get("service")
            and service_from_job(str(rule.get("expr", "")), jobs)[0]
        )
        problems += rule_test_problems(stem, alerts, tests.get(stem), from_job)
    return problems
