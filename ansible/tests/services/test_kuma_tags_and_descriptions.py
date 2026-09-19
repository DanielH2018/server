"""The description and tag conventions the notification templates read (#2065, #2066).

Split from test_kuma_static_monitors.py, which sits at the 500-line module cap. Tag references
are the same NameNotFound hazard as the notification reference: a monitor naming a tag the
Secret does not declare fails to parse and, under ON_DELETE=delete, is deleted (#2076).
"""

from _helpers import ANSIBLE
from _kuma_entities import _entities
from test_kuma_static_monitors import EMAIL_TIER, NOT_MONITORS


def _tag_values(entity, tag):
    return [t["value"] for t in entity.get("tag_names", []) if t["name"] == tag]


def test_every_push_tile_carries_a_description():
    # A push tile's name is all a reader has — no URL says what it probes. The Discord and
    # email templates print `description` under the title and Kuma renders it on the
    # monitor page; both omit it silently when absent, so the gap is invisible from the
    # output. An http/port/dns tile's target is its own description.
    missing = sorted(
        e["name"]
        for e in _entities().values()
        if e["type"] == "push" and not e.get("description", "").strip()
    )
    assert not missing, f"push tiles with no description: {missing}"


def test_every_tag_a_monitor_names_is_a_declared_tag_entity():
    """A monitor naming a tag the Secret does not declare is not a monitor with a typo.

    AutoKuma's `resolve_names` raises NameNotFound, the monitor fails to parse, and under
    `ON_DELETE=delete` a monitor that fails to parse is one that was removed — the mechanism
    that deleted all 107 monitors on 2026-09-18 when the notification failed to parse (#2076).
    """
    entities = _entities()
    declared = {e["name"] for e in entities.values() if e["type"] == "tag"}
    assert declared == {"severity", "runbook"}
    for name, entity in entities.items():
        for tag in entity.get("tag_names", []):
            assert tag["name"] in declared, (
                f"{name}: names undeclared tag {tag['name']!r}"
            )
            assert tag.get("value"), f"{name}: tag {tag['name']!r} carries no value"


def test_severity_critical_is_exactly_the_email_tier():
    # The two are one fact stated twice: the tier that survives a degraded Discord is the
    # tier the templates label critical. Keep them from drifting apart.
    critical = {
        e["name"]
        for e in _entities().values()
        if e["type"] not in NOT_MONITORS and "critical" in _tag_values(e, "severity")
    }
    assert critical == EMAIL_TIER


def test_every_runbook_tag_points_at_a_page_the_docs_site_serves():
    # A runbook link on an alert that 404s is worse than none. The docs site serves
    # docs/<name>.md at /<name>/ (mkdocs, use_directory_urls: true).
    docs = ANSIBLE.parent / "docs"
    for name, entity in _entities().items():
        for url in _tag_values(entity, "runbook"):
            prefix = "https://docs.local.example.com/"
            assert url.startswith(prefix), (
                f"{name}: runbook is not on the docs site: {url}"
            )
            page = url.removeprefix(prefix).strip("/")
            assert (docs / f"{page}.md").is_file(), (
                f"{name}: no docs/{page}.md for {url}"
            )
