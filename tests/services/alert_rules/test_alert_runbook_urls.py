"""Every alert that cites a runbook page links it as a `runbook_url` on the docs site.

The alert plane (ADR-0026) sets `runbook_url` wherever a runbook exists, and
`files/alertmanager/homelab.tmpl` prints it as the notification's `Runbook:` line. A
description that names `docs/<page>.md` gives a reader on a phone a path they cannot open, so
each such alert also carries the URL. A URL may name a section as `<page>/#<anchor>`, and the
anchor must be a heading on the page.

Every critical rule links a runbook. Every alert's description fits DESCRIPTION_CAP, warnings
included: a warning reaches Discord and, through `WarningUnresolved` after 48 hours, a phone.
The notification says what to do first, and the runbook (most often `docs/alert-runbooks.md`)
holds the edge cases.

Every alert's summary reads `<service or host>: <what is wrong>`. The notification prints one
summary per alert, so a reader scanning a list finds the thing that broke in the same place on
every line. The subject is the rule's own `service` label, an inventory host, or the firing
series' labels (`{{ $labels.namespace }}/{{ $labels.pod }}`) where one of them names the thing
more exactly than the rule's `service` does. Every summary also fits SUMMARY_CAP once
rendered. The cap measures the text a promtool test's `exp_annotations` expects the alert to
render, because a `{{ with query ... }}` template runs to hundreds of characters of source and
renders one short line. A summary with no template, or one no test renders, is measured as
its source.

DECIDED: runbook_url is a literal URL in each rule, not a page slug that the Alertmanager
template expands. The rule files are plain YAML that Jinja never renders, so the domain is a
literal there, and this module pins it to `domain` in `estate/vars.yml`. A slug would need
a second Jinja-rendered template in the alertmanager-config Secret, and the same define in
amtool's offline render, for a domain the public repo already names in many files.

Run: uv run pytest tests/services/alert_rules/test_alert_runbook_urls.py
"""

import re

from lib.estate import shared_vars
from _helpers import K8S_ROLES
from lib import yaml_fast
from lib.inventory_lists import host_names
from lib.repo_paths import REPO

# The shipped rules of every role, Prometheus' and loki-homelab's ruler alike.
RULE_FILES = sorted(K8S_ROLES.glob("*/files/rules/*.yaml"))
RULE_TESTS = sorted(K8S_ROLES.glob("*/tests/rules/*.test.yaml"))
# Past this the notification turns into an essay on a phone; edge cases belong in the runbook.
DESCRIPTION_CAP = 300
# The longest rendered summary, in characters: one line of a notification listing many alerts.
SUMMARY_CAP = 120
# Non-vacuity: alerts whose summary source is a query template over SUMMARY_CAP.
TEMPLATED = frozenset({"K3sNodeVersionSkew", "FluxNotReady"})
# Non-vacuity: critical rules the census must find, one of them loki-homelab's.
CRITICAL = frozenset({"WANUnreachable", "HostFilesystemRemountedReadOnly"})
DOCS = REPO / "docs"
# Archived design docs explain why a rule exists; they are not where a paged reader goes.
CITED_PAGE = re.compile(r"(?<![\w/.-])docs/((?!archive/)[\w/-]+)\.md\b")
# Non-vacuity: an alert that must link its runbook, and the page it links.
NAMED = ("HostReconcileFailed", "host-packages")
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.M)
_LABEL = r"\{\{ \$labels\.\w+ \}\}"
# A summary's subject and the `: ` that ends it: one name, or label values joined by `/`.
SUBJECT = re.compile(rf"(?:(?P<name>[\w.-]+)|{_LABEL}(?:/{_LABEL})*): \S")


def _base_url() -> str:
    return f"https://docs.local.{shared_vars()['domain']}/"


def anchors(page: str) -> set[str]:
    """The heading anchors MkDocs' toc extension gives `docs/<page>.md`."""
    text = (DOCS / f"{page}.md").read_text()
    text = re.sub(r"^```.*?^```", "", text, flags=re.M | re.S)
    slugs = set()
    for heading in HEADING.findall(text):
        plain = re.sub(r"[^\w\s-]", "", heading.replace("`", "")).strip().lower()
        slugs.add(re.sub(r"[-\s]+", "-", plain))
    return slugs


def _alerts() -> list[tuple[str, dict]]:
    """Each alert rule as (its file's name, the rule)."""
    return [
        (path.name, rule)
        for path in RULE_FILES
        for group in yaml_fast.safe_load(path.read_text())["groups"]
        for rule in group["rules"]
        if "alert" in rule
    ]


def _rendered_summaries() -> dict[str, list[str]]:
    """Each summary a promtool test expects an alert to render, by alert name."""
    rendered: dict[str, list[str]] = {}
    for path in RULE_TESTS:
        for case in yaml_fast.safe_load(path.read_text())["tests"]:
            for check in case.get("alert_rule_test") or []:
                for alert in check.get("exp_alerts") or []:
                    summary = (alert.get("exp_annotations") or {}).get("summary")
                    if summary is not None:
                        rendered.setdefault(check["alertname"], []).append(summary)
    return rendered


def summary_length_problems(
    alerts: list[tuple[str, dict]], rendered: dict[str, list[str]]
) -> list[str]:
    """Why an alert's summary runs past SUMMARY_CAP, or nothing.

    Args:
        alerts: (file name, rule) for each alert rule.
        rendered: The rendered summaries of each alert a test renders, by alert name. A
            rule shares them with every rule of its name, so they stand in only for a
            templated summary; a plain one is its own render and is measured as written.
    """
    problems = []
    for name, rule in alerts:
        source = (rule.get("annotations") or {}).get("summary", "")
        templated = "{{" in source
        for summary in rendered.get(rule["alert"], [source]) if templated else [source]:
            if len(summary) > SUMMARY_CAP:
                problems.append(
                    f"{name}/{rule['alert']}: the summary {summary!r} is {len(summary)} "
                    f"characters, over the {SUMMARY_CAP} cap"
                )
    return problems


def runbook_problems(alerts: list[tuple[str, dict]], base: str) -> list[str]:
    """Why an alert's runbook link is wrong or missing, or nothing.

    Args:
        alerts: (file name, rule) for each alert rule.
        base: The docs site's URL, ending in `/`.
    """
    problems = []
    for name, rule in alerts:
        where = f"{name}/{rule['alert']}"
        annotations = rule.get("annotations") or {}
        url = annotations.get("runbook_url")
        cited = set(CITED_PAGE.findall(annotations.get("description", "")))
        if url is None:
            if cited:
                problems.append(
                    f"{where}: the description cites docs/{sorted(cited)[0]}.md but the "
                    f"alert sets no runbook_url; add {base}{sorted(cited)[0]}/"
                )
            continue
        path, _, anchor = url.partition("#")
        page = path.removeprefix(base).removesuffix("/")
        if not (path.startswith(base) and path.endswith("/")):
            problems.append(
                f"{where}: runbook_url {url} is not {base}<page>/[#<anchor>]"
            )
        elif not (DOCS / f"{page}.md").is_file():
            problems.append(f"{where}: runbook_url {url} names no docs/{page}.md")
        elif anchor and anchor not in anchors(page):
            problems.append(
                f"{where}: runbook_url {url} names no heading of docs/{page}.md"
            )
        elif cited and page not in cited:
            problems.append(
                f"{where}: runbook_url links {page}, but the description cites "
                f"{sorted(cited)}"
            )
    return problems


def notification_problems(alerts: list[tuple[str, dict]]) -> list[str]:
    """Why a critical rule has no runbook, or any alert runs past DESCRIPTION_CAP, or nothing."""
    problems = []
    for name, rule in alerts:
        where = f"{name}/{rule['alert']}"
        annotations = rule.get("annotations") or {}
        critical = (rule.get("labels") or {}).get("severity") == "critical"
        if critical and "runbook_url" not in annotations:
            problems.append(f"{where}: a critical rule with no runbook_url")
        length = len(annotations.get("description", ""))
        if length > DESCRIPTION_CAP:
            problems.append(
                f"{where}: the description is {length} characters, over the "
                f"{DESCRIPTION_CAP} cap; move the edge cases to the runbook"
            )
    return problems


def summary_problems(
    alerts: list[tuple[str, dict]], hosts: tuple[str, ...]
) -> list[str]:
    """Why an alert's summary is not `<service or host>: <what is wrong>`, or nothing.

    Args:
        alerts: (file name, rule) for each alert rule.
        hosts: The inventory hosts a summary may name as its subject.
    """
    problems = []
    for name, rule in alerts:
        where = f"{name}/{rule['alert']}"
        summary = (rule.get("annotations") or {}).get("summary", "")
        service = (rule.get("labels") or {}).get("service")
        subject = SUBJECT.match(summary)
        if subject is None:
            problems.append(
                f"{where}: the summary {summary!r} does not open with its service or host "
                "and `: `"
            )
        elif subject["name"] not in (None, service, *hosts):
            problems.append(
                f"{where}: the summary's subject {subject['name']!r} is neither the rule's "
                f"service {service!r} nor an inventory host"
            )
    return problems


def test_every_critical_rule_links_a_runbook_and_every_alert_fits_the_cap_is_clean():
    alerts = _alerts()
    critical = {
        rule["alert"]
        for _name, rule in alerts
        if (rule.get("labels") or {}).get("severity") == "critical"
    }
    assert CRITICAL <= critical, sorted(CRITICAL - critical)
    assert notification_problems(alerts) == []


def test_a_critical_rule_with_no_runbook_url_is_flagged():
    rule = {"alert": "X", "labels": {"severity": "critical"}, "annotations": {}}
    assert notification_problems([("x.yaml", rule)]) == [
        "x.yaml/X: a critical rule with no runbook_url"
    ]


def test_a_critical_description_over_the_cap_is_flagged():
    annotations = {"description": "a" * (DESCRIPTION_CAP + 1), "runbook_url": "u"}
    rule = {
        "alert": "X",
        "labels": {"severity": "critical"},
        "annotations": annotations,
    }
    assert notification_problems([("x.yaml", rule)]) == [
        f"x.yaml/X: the description is {DESCRIPTION_CAP + 1} characters, over the "
        f"{DESCRIPTION_CAP} cap; move the edge cases to the runbook"
    ]


def test_a_warning_without_a_runbook_is_clean():
    rule = {"alert": "X", "labels": {"severity": "warning"}, "annotations": {}}
    assert notification_problems([("x.yaml", rule)]) == []


def test_a_warning_description_over_the_cap_is_flagged():
    rule = {
        "alert": "X",
        "labels": {"severity": "warning"},
        "annotations": {"description": "a" * (DESCRIPTION_CAP + 1)},
    }
    assert notification_problems([("x.yaml", rule)]) == [
        f"x.yaml/X: the description is {DESCRIPTION_CAP + 1} characters, over the "
        f"{DESCRIPTION_CAP} cap; move the edge cases to the runbook"
    ]


def _summarised(
    summary: str, service: str | None = "longhorn"
) -> list[tuple[str, dict]]:
    rule = {"alert": "X", "annotations": {"summary": summary}}
    if service is not None:
        rule["labels"] = {"service": service}
    return [("x.yaml", rule)]


HOSTS = ("daniel-box",)


def test_every_alert_summary_opens_with_its_service_or_host_is_clean():
    alerts = _alerts()
    assert CRITICAL <= {rule["alert"] for _name, rule in alerts}
    assert summary_problems(alerts, host_names()) == []


def test_every_alert_summary_fits_the_cap_once_rendered_is_clean():
    # fact: k8s/roles/observability/CLAUDE.md#Alert plane
    alerts = _alerts()
    rendered = _rendered_summaries()
    sources = {
        rule["alert"]: (rule.get("annotations") or {}).get("summary", "")
        for _name, rule in alerts
    }
    assert all(len(sources[alert]) > SUMMARY_CAP for alert in TEMPLATED)
    assert TEMPLATED <= rendered.keys(), sorted(TEMPLATED - rendered.keys())
    assert summary_length_problems(alerts, rendered) == []


def test_a_rendered_summary_over_the_cap_is_flagged():
    summary = "longhorn: " + "a" * SUMMARY_CAP
    rendered = {"X": ["longhorn: short.", summary]}
    assert summary_length_problems(_summarised("{{ x }}"), rendered) == [
        f"x.yaml/X: the summary {summary!r} is {len(summary)} characters, over the "
        f"{SUMMARY_CAP} cap"
    ]


def test_a_long_template_that_renders_short_is_clean():
    template = "longhorn: {{ with query " + "a" * SUMMARY_CAP + " }}{{ end }}"
    rendered = {"X": ["longhorn: the volume is degraded."]}
    assert summary_length_problems(_summarised(template), rendered) == []


def test_a_plain_summary_over_the_cap_is_flagged_beside_a_short_render():
    summary = "longhorn: " + "a" * SUMMARY_CAP
    rendered = {"X": ["longhorn: short."]}
    assert summary_length_problems(_summarised(summary), rendered) == [
        f"x.yaml/X: the summary {summary!r} is {len(summary)} characters, over the "
        f"{SUMMARY_CAP} cap"
    ]


def test_a_summary_no_test_renders_is_measured_as_its_source():
    summary = "longhorn: " + "a" * SUMMARY_CAP
    assert summary_length_problems(_summarised(summary), {}) == [
        f"x.yaml/X: the summary {summary!r} is {len(summary)} characters, over the "
        f"{SUMMARY_CAP} cap"
    ]


def test_a_summary_that_opens_with_the_symptom_is_flagged():
    summary = "Failed system units on daniel-box: a.service."
    assert summary_problems(_summarised(summary), HOSTS) == [
        f"x.yaml/X: the summary {summary!r} does not open with its service or host and `: `"
    ]


def test_a_summary_whose_subject_is_another_service_is_flagged():
    assert summary_problems(_summarised("traefik: the volume is degraded."), HOSTS) == [
        "x.yaml/X: the summary's subject 'traefik' is neither the rule's service "
        "'longhorn' nor an inventory host"
    ]


def test_a_summary_that_opens_with_the_rules_service_or_a_host_is_clean():
    alerts = _summarised("longhorn: the volume is degraded.") + _summarised(
        "daniel-box: the disk is past the scheduling floor."
    )
    assert summary_problems(alerts, HOSTS) == []


def test_a_summary_whose_subject_is_the_series_labels_is_clean():
    summary = "{{ $labels.namespace }}/{{ $labels.pod }}: a container was OOM-killed."
    assert summary_problems(_summarised(summary, service=None), HOSTS) == []


def test_every_alert_links_the_runbook_its_description_cites_is_clean():
    assert runbook_problems(_alerts(), _base_url()) == []


def test_the_named_alert_links_its_runbook():
    alert, page = NAMED
    urls = {
        (rule.get("annotations") or {}).get("runbook_url")
        for _name, rule in _alerts()
        if rule["alert"] == alert
    }
    assert urls == {f"{_base_url()}{page}/"}


def _rule(description: str, url: str | None = None) -> list[tuple[str, dict]]:
    annotations = {"description": description}
    if url is not None:
        annotations["runbook_url"] = url
    return [("x.yaml", {"alert": "X", "annotations": annotations})]


BASE = "https://docs.local.example.com/"


def test_a_cited_page_with_no_runbook_url_is_flagged():
    assert runbook_problems(_rule("Read docs/host-packages.md."), BASE) == [
        "x.yaml/X: the description cites docs/host-packages.md but the alert sets no "
        f"runbook_url; add {BASE}host-packages/"
    ]


def test_a_design_doc_citation_with_no_runbook_url_is_clean():
    cited = "See docs/archive/monitoring/m1-alertmanager.md."
    assert runbook_problems(_rule(cited), BASE) == []


def test_a_role_docs_citation_with_no_runbook_url_is_clean():
    cited = "k8s/roles/home-assistant/docs/platform.md describes the loop."
    assert runbook_problems(_rule(cited), BASE) == []


def test_a_runbook_url_on_another_host_is_flagged():
    url = "https://docs.local.other.org/host-packages/"
    assert runbook_problems(_rule("Read it.", url), BASE) == [
        f"x.yaml/X: runbook_url {url} is not {BASE}<page>/[#<anchor>]"
    ]


def test_a_runbook_url_naming_no_page_is_flagged():
    url = f"{BASE}no-such-page/"
    assert runbook_problems(_rule("Read it.", url), BASE) == [
        f"x.yaml/X: runbook_url {url} names no docs/no-such-page.md"
    ]


def test_a_runbook_url_naming_an_existing_heading_is_clean():
    url = f"{_base_url()}alert-runbooks/#wanunreachable"
    assert runbook_problems(_rule("Read it.", url), _base_url()) == []


def test_a_runbook_url_naming_no_heading_is_flagged():
    url = f"{_base_url()}alert-runbooks/#no-such-alert"
    assert runbook_problems(_rule("Read it.", url), _base_url()) == [
        f"x.yaml/X: runbook_url {url} names no heading of docs/alert-runbooks.md"
    ]


def test_a_runbook_url_linking_another_page_than_the_cited_one_is_flagged():
    problems = runbook_problems(
        _rule("Read docs/k3s-upgrade.md.", f"{BASE}host-packages/"), BASE
    )
    assert problems == [
        "x.yaml/X: runbook_url links host-packages, but the description cites "
        "['k3s-upgrade']"
    ]
