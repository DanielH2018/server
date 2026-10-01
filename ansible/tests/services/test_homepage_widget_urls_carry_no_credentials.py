#!/usr/bin/env python3
"""A credential in a homepage widget URL ends up in the pod log, and from there in Loki.

Homepage's widget proxies log the FULL request URL on every non-2xx response — `logger.error(
"HTTP Error %d calling %s", status, url.toString())` in each `proxy.js`. A widget whose URL
carries its credential as a query parameter therefore publishes that credential on the target's
next outage, to the pod log, to Loki, and to any transcript that reads either. Jellyfin's v1
widget mappings are `emby/Sessions?api_key={key}`, which would leak `jellyfin_api_key` on every
failed refresh.

The rule this pins: a widget's credential goes in `key:`, which the proxy turns into a header,
never into the `url:`. A re-added jellyfin widget must set `version: 2` for the same reason —
`SessionsV2`/`CountV2` carry no `api_key` and the proxy authenticates with
`Authorization: MediaBrowser Token=...` instead.

Not a jellyfin-shaped guard on purpose: "no jellyfin widget without `version: 2`" iterates an
empty set today, since the tile is dropped, and would pass forever.

Reads the RENDERED `services.yaml` out of homepage's config Secret (`_homepage_config`) rather
than scanning `services.yaml.j2` for `url:` lines. The rendered file is ordinary YAML, so every
`url` at any depth is reachable by walking the parsed document — including the calendar widget's
`integrations[].url`, which a line scan found only because it happened to spell the key the same
way. A secret interpolated into a URL renders as a placeholder here, and the check is on the
PARAMETER NAME rather than its value, so `?api_key={{ jellyfin_api_key }}` is still caught: it
renders as `?api_key=<placeholder>`. What neither form can see is a credential carried inside a
secret's own value, because that value is not in the repo.

Run: uv run pytest ansible/tests/services/test_homepage_widget_urls_carry_no_credentials.py
"""

import re

from _homepage_config import config_urls, domain, namespace

# A query parameter whose NAME says it carries a credential. `?query=` (the Headlamp tile's
# PromQL) is not one of these, and must stay unflagged.
CREDENTIAL_PARAM = re.compile(
    r"[?&](api_?key|apikey|token|access_token|password|passwd|secret|auth)=", re.I
)


def known_urls() -> frozenset[str]:
    """Non-vacuity: URLs the census must keep finding, at their RENDERED values.

    A reshaped tile list would otherwise leave the census empty, and an `all()` over nothing
    passes. The hostname's domain and namespace come from the render context rather than being
    written out, so a member breaks when the TILE changes and not when the harness's stub
    inventory does — the service name and port are the part this is pinning.
    """
    return frozenset(
        {
            f"https://uptime-kuma.local.{domain()}",
            f"http://scrutiny.{namespace()}.svc.cluster.local:8080",
            f"http://sonarr.{namespace()}.svc.cluster.local:8989",
        }
    )


def urls_carrying_credentials(urls: set[str]) -> set[str]:
    """The URLs that would publish a credential into the pod log on a widget error."""
    return {u for u in urls if CREDENTIAL_PARAM.search(u)}


def test_no_widget_url_carries_a_credential_in_its_query_string():
    """The accepting half, against the real render."""
    assert urls_carrying_credentials(config_urls()) == set()


def test_the_census_still_finds_the_urls_it_is_meant_to_cover():
    """Non-vacuity. The test above passes on an empty census."""
    assert known_urls() <= config_urls()


def test_the_census_reaches_a_nested_integration_url():
    """The calendar widget's URLs sit under `integrations:`, not beside the widget `type:`."""
    nested = {
        "widget": {
            "type": "calendar",
            "integrations": [{"type": "ical", "url": "http://ical-proxy/one.ics"}],
        }
    }
    assert config_urls(nested) == {"http://ical-proxy/one.ics"}


def test_a_url_carrying_an_api_key_is_flagged():
    """The rejecting half. A pattern that matched nothing would pass both tests above."""
    leaky = "https://jellyfin.local.example.com/emby/Sessions?api_key=abc123"
    assert urls_carrying_credentials({leaky}) == {leaky}


def test_a_promql_query_parameter_is_not_a_credential():
    """The Headlamp tile's `?query=` is the one query string this rule must leave alone."""
    headlamp = (
        "http://prometheus.observability.svc.cluster.local:9090"
        "/api/v1/query?query=count%28kube_pod_status_phase%29"
    )
    assert urls_carrying_credentials({headlamp}) == set()
