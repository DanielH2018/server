"""Every alert that cites a runbook page links it as a `runbook_url` on the docs site.

The alert plane (ADR-0026) sets `runbook_url` wherever a runbook exists, and
`files/alertmanager/homelab.tmpl` prints it as the notification's `Runbook:` line. A
description that names `docs/<page>.md` gives a reader on a phone a path they cannot open, so
each such alert also carries the URL. A URL may name a section as `<page>/#<anchor>`, and the
anchor must be a heading on the page.

Every critical rule links a runbook, and its description fits DESCRIPTION_CAP: a page that
reaches a phone says what to do first, and the runbook (most often `docs/alert-runbooks.md`)
holds the edge cases.

DECIDED: runbook_url is a literal URL in each rule, not a page slug that the Alertmanager
template expands. The rule files are plain YAML that Jinja never renders, so the domain is a
literal there, and this module pins it to `domain` in `group_vars/all.yml`. A slug would need
a second Jinja-rendered template in the alertmanager-config Secret, and the same define in
amtool's offline render, for a domain the public repo already names in many files.

Run: uv run pytest tests/services/test_alert_runbook_urls.py
"""

import re

from _helpers import K8S_ROLES
from lib import yaml_fast
from lib.repo_paths import ALL_VARS, REPO

ROLE = K8S_ROLES / "observability"
# The shipped rules of every role, Prometheus' and loki-homelab's ruler alike, and the fixture
# rule host-checks.test.yaml fires beside them.
RULE_FILES = sorted(K8S_ROLES.glob("*/files/rules/*.yaml")) + sorted(
    (ROLE / "tests" / "rules" / "fixtures").glob("*.yaml")
)
# The longest description a critical rule's source may carry, in characters. Past it the
# notification turns into an essay on a phone, and the edge cases belong in the runbook.
DESCRIPTION_CAP = 300
# Non-vacuity: critical rules the census must find, one of them loki-homelab's.
CRITICAL = frozenset({"WANUnreachable", "HostFilesystemRemountedReadOnly"})
DOCS = REPO / "docs"
# A page the description cites as where to go next. The archived design docs under archive/
# explain why a rule exists; they are not where a paged reader goes.
CITED_PAGE = re.compile(r"(?<![\w/.-])docs/((?!archive/)[\w/-]+)\.md\b")
# Non-vacuity: an alert that must link its runbook, and the page it links.
NAMED = ("HostReconcileFailed", "host-packages")
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.M)


def _base_url() -> str:
    return f"https://docs.local.{yaml_fast.safe_load(ALL_VARS.read_text())['domain']}/"


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


def critical_problems(alerts: list[tuple[str, dict]]) -> list[str]:
    """Why a critical rule pages without a runbook or past DESCRIPTION_CAP, or nothing."""
    problems = []
    for name, rule in alerts:
        if (rule.get("labels") or {}).get("severity") != "critical":
            continue
        where = f"{name}/{rule['alert']}"
        annotations = rule.get("annotations") or {}
        if "runbook_url" not in annotations:
            problems.append(f"{where}: a critical rule with no runbook_url")
        length = len(annotations.get("description", ""))
        if length > DESCRIPTION_CAP:
            problems.append(
                f"{where}: the description is {length} characters, over the "
                f"{DESCRIPTION_CAP} cap; move the edge cases to the runbook"
            )
    return problems


def test_every_critical_rule_links_a_runbook_and_fits_the_cap_is_clean():
    alerts = _alerts()
    critical = {
        rule["alert"]
        for _name, rule in alerts
        if (rule.get("labels") or {}).get("severity") == "critical"
    }
    assert CRITICAL <= critical, sorted(CRITICAL - critical)
    assert critical_problems(alerts) == []


def test_a_critical_rule_with_no_runbook_url_is_flagged():
    rule = {"alert": "X", "labels": {"severity": "critical"}, "annotations": {}}
    assert critical_problems([("x.yaml", rule)]) == [
        "x.yaml/X: a critical rule with no runbook_url"
    ]


def test_a_critical_description_over_the_cap_is_flagged():
    annotations = {"description": "a" * (DESCRIPTION_CAP + 1), "runbook_url": "u"}
    rule = {
        "alert": "X",
        "labels": {"severity": "critical"},
        "annotations": annotations,
    }
    assert critical_problems([("x.yaml", rule)]) == [
        f"x.yaml/X: the description is {DESCRIPTION_CAP + 1} characters, over the "
        f"{DESCRIPTION_CAP} cap; move the edge cases to the runbook"
    ]


def test_a_warning_without_a_runbook_or_over_the_cap_is_clean():
    rule = {
        "alert": "X",
        "labels": {"severity": "warning"},
        "annotations": {"description": "a" * (DESCRIPTION_CAP + 1)},
    }
    assert critical_problems([("x.yaml", rule)]) == []


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
