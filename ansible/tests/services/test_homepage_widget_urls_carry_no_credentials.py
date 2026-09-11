#!/usr/bin/env python3
"""A credential in a homepage widget URL ends up in the pod log, and from there in Loki.

Homepage's widget proxies log the FULL request URL on every non-2xx response — `logger.error(
"HTTP Error %d calling %s", status, url.toString())` in each `proxy.js`. A widget whose URL
carries its credential as a query parameter therefore publishes that credential on the target's
next outage, to the pod log, to Loki, and to any transcript that reads either. That is exactly
how `jellyfin_api_key` leaked during the 2026-09-09 Jellyfin crash loop (#1499): the jellyfin
widget's v1 mappings are `emby/Sessions?api_key={key}`, and the tile 404'd every refresh (#1457).

The rule this pins: a widget's credential goes in `key:`, which the proxy turns into a header,
never into the `url:`. A re-added jellyfin widget must set `version: 2` for the same reason —
`SessionsV2`/`CountV2` carry no `api_key` and the proxy authenticates with
`Authorization: MediaBrowser Token=...` instead.

Not a jellyfin-shaped guard on purpose: "no jellyfin widget without `version: 2`" iterates an
empty set today, since the tile is dropped, and would pass forever.

Run: uv run pytest ansible/tests/services/test_homepage_widget_urls_carry_no_credentials.py
"""

import re

from _helpers import ANSIBLE as _ANSIBLE

SERVICES_TEMPLATE = (
    _ANSIBLE / "roles" / "k8s" / "homepage" / "templates" / "services.yaml.j2"
)

# Every `url:` value in the tile list, widget or calendar alike.
URL_LINE = re.compile(r"^\s*url:\s*(\S.*?)\s*(?:#.*)?$", re.MULTILINE)

# A query parameter whose NAME says it carries a credential. `?query=` (the Headlamp tile's
# PromQL) is not one of these, and must stay unflagged.
CREDENTIAL_PARAM = re.compile(
    r"[?&](api_?key|apikey|token|access_token|password|passwd|secret|auth)=", re.I
)

# Non-vacuity: named URLs the census must keep finding. A reshaped `url:` line would otherwise
# leave the census empty, and an `all()` over nothing passes.
KNOWN_URLS = frozenset(
    {
        "https://uptime-kuma.local.{{ domain }}",
        "http://scrutiny.{{ k8s_namespace }}.svc.cluster.local:8080",
        "http://sonarr.{{ k8s_namespace }}.svc.cluster.local:8989",
    }
)


def widget_urls(template_text: str) -> set[str]:
    """Every URL the tile list hands to homepage, with surrounding quotes stripped."""
    return {m.group(1).strip("\"'") for m in URL_LINE.finditer(template_text)}


def urls_carrying_credentials(urls: set[str]) -> set[str]:
    """The URLs that would publish a credential into the pod log on a widget error."""
    return {u for u in urls if CREDENTIAL_PARAM.search(u)}


def test_no_widget_url_carries_a_credential_in_its_query_string():
    """The accepting half, against the real tree."""
    assert (
        urls_carrying_credentials(widget_urls(SERVICES_TEMPLATE.read_text())) == set()
    )


def test_the_census_still_finds_the_urls_it_is_meant_to_cover():
    """Non-vacuity. The test above passes on an empty census."""
    assert KNOWN_URLS <= widget_urls(SERVICES_TEMPLATE.read_text())


def test_a_url_carrying_an_api_key_is_flagged():
    """The rejecting half. A pattern that matched nothing would pass both tests above."""
    leaky = "https://jellyfin.local.{{ domain }}/emby/Sessions?api_key={{ jellyfin_api_key }}"
    assert urls_carrying_credentials({leaky}) == {leaky}


def test_a_promql_query_parameter_is_not_a_credential():
    """The Headlamp tile's `?query=` is the one query string this rule must leave alone."""
    headlamp = (
        "http://prometheus.{{ k8s_observability_namespace }}.svc.cluster.local:9090"
        "/api/v1/query?query={{ homepage_k8s_headlamp_cluster_query | urlencode }}"
    )
    assert urls_carrying_credentials({headlamp}) == set()
