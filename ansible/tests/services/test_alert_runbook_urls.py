"""Every alert that cites a runbook page links it as a `runbook_url` on the docs site.

M1 convention 4 (`docs/target-architecture/monitoring/m1-alertmanager.md`, *Annotations*)
asks for `runbook_url` wherever a runbook exists, and `files/alertmanager/homelab.tmpl` prints
it as the notification's `Runbook:` line. A description that names `docs/<page>.md` gives a
reader on a phone a path they cannot open, so each such alert also carries the URL (#6174).

DECIDED: runbook_url is a literal URL in each rule, not a page slug that the Alertmanager
template expands. The rule files are plain YAML that Jinja never renders, so the domain is a
literal there, and this module pins it to `domain` in `group_vars/all.yml`. A slug would need
a second Jinja-rendered template in the alertmanager-config Secret, and the same define in
amtool's offline render, for a domain the public repo already names in many files.

Run: uv run pytest ansible/tests/services/test_alert_runbook_urls.py
"""

import re

from _helpers import K8S_ROLES
from lib import yaml_fast
from lib.repo_paths import ALL_VARS, REPO

ROLE = K8S_ROLES / "observability"
# The shipped rules, and the fixture rule host-checks.test.yaml fires beside them.
RULE_FILES = sorted((ROLE / "files" / "rules").glob("*.yaml")) + sorted(
    (ROLE / "tests" / "rules" / "fixtures").glob("*.yaml")
)
DOCS = REPO / "docs"
# A page the description cites as where to go next. Design docs under these directories
# explain why a rule exists; they are not where a paged reader goes.
CITED_PAGE = re.compile(r"\bdocs/((?!target-architecture/|archive/)[\w/-]+)\.md\b")
# Non-vacuity: an alert that must link its runbook, and the page it links.
NAMED = ("HostReconcileFailed", "host-packages")


def _base_url() -> str:
    return f"https://docs.local.{yaml_fast.safe_load(ALL_VARS.read_text())['domain']}/"


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
        page = url.removeprefix(base).removesuffix("/")
        if not (url.startswith(base) and url.endswith("/")):
            problems.append(f"{where}: runbook_url {url} is not {base}<page>/")
        elif not (DOCS / f"{page}.md").is_file():
            problems.append(f"{where}: runbook_url {url} names no docs/{page}.md")
        elif cited and page not in cited:
            problems.append(
                f"{where}: runbook_url links {page}, but the description cites "
                f"{sorted(cited)}"
            )
    return problems


def test_every_alert_links_the_runbook_its_description_cites():
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


def test_a_design_doc_citation_needs_no_runbook_url():
    cited = "See docs/target-architecture/monitoring/m1-alertmanager.md."
    assert runbook_problems(_rule(cited), BASE) == []


def test_a_runbook_url_on_another_host_is_flagged():
    url = "https://docs.local.other.org/host-packages/"
    assert runbook_problems(_rule("Read it.", url), BASE) == [
        f"x.yaml/X: runbook_url {url} is not {BASE}<page>/"
    ]


def test_a_runbook_url_naming_no_page_is_flagged():
    url = f"{BASE}no-such-page/"
    assert runbook_problems(_rule("Read it.", url), BASE) == [
        f"x.yaml/X: runbook_url {url} names no docs/no-such-page.md"
    ]


def test_a_runbook_url_linking_another_page_than_the_cited_one_is_flagged():
    problems = runbook_problems(
        _rule("Read docs/k3s-upgrade.md.", f"{BASE}host-packages/"), BASE
    )
    assert problems == [
        "x.yaml/X: runbook_url links host-packages, but the description cites "
        "['k3s-upgrade']"
    ]
