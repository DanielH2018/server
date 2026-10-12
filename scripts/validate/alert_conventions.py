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
- every alert carries a literal `service`, or takes it from its expression through M2's join
  shape (`service_from_mapping`): a `group_left(service)` join to a workload-service recording
  rule, with a `label_replace` that maps a workload the join misses to `service="unmapped"`;
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
groups and ConfigMap keys it reads from the role's render. This module renders nothing.
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


def rule_file_problems(stem: str, doc: object) -> list[str]:
    """A problem for each convention one parsed rule file breaks on its own."""
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
            reason = service_from_mapping(str(rule.get("expr", "")))
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


def rule_test_problems(stem: str, alerts: list[str], tests: object) -> list[str]:
    """A problem for each alert no case fires, and for a family with no zero-series case.

    Args:
        stem: The rule file's name without `.yaml`.
        alerts: The alert names that rule file holds.
        tests: The parsed `<stem>.test.yaml`, or None when it is missing.
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
    zero_series = False
    for case in cases:
        for check in case.get("alert_rule_test") or []:
            expected = check.get("exp_alerts") or []
            if not expected:
                continue
            fired.add(check.get("alertname"))
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
) -> list[str]:
    """A problem for each rule convention the role's rule files and their tests break.

    Args:
        groups: `observability_rule_groups`.
        keys: The rendered `prometheus-rules` ConfigMap's data keys.
        rules_dir: The rule files.
        tests_dir: Their promtool tests.
        templates_file: The Alertmanager template source, checked for `${` with the rules.
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
    for stem, doc in docs.items():
        problems += rule_file_problems(stem, doc)
        alerts = [rule["alert"] for _group, rule in _alerts(doc)]
        problems += rule_test_problems(stem, alerts, tests.get(stem))
    return problems
