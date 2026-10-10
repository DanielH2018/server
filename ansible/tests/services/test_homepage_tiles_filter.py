"""Unit tests for filter_plugins/homepage_tiles.py.

`homepage_widget: true` on a containers_list entry both opens the entry's fence to homepage
(`netpol_callers`) and is how homepage's tile list builds a ClusterIP widget URL
(`homepage_widget_url`). Refusing an unflagged entry keeps the two halves equal (#3691). That
the fences actually render the derived callers is test_netpol_from.py's job.

The filter guards only the URLs written through it. A tile that hand-writes
`http://jellyfin.{{ k8s_namespace }}.svc.cluster.local:8096` bypasses it, so the census at the
end of this module walks the RENDERED tile list. Every URL dialling a Service in this
namespace, by FQDN or by bare name, must name an entry carrying the key or a bespoke fence in
BESPOKE_FENCES that admits homepage. A URL naming another namespace (Headlamp's Prometheus
query) is out of scope: its fence is cross-namespace and bespoke.

Run: uv run pytest ansible/tests/services/test_homepage_tiles_filter.py
"""

import re

import pytest

from _homepage_config import config_urls, namespace
from _k8s_render import host_context, rendered_docs
from homepage_tiles import homepage_href, homepage_widget_url, netpol_callers
from lib.ansible_inventory import containers_entries_in

ENTRIES = [
    {"name": "sonarr", "hostname": "sonarr", "port": 8989, "homepage_widget": True},
    {"name": "jellyfin", "hostname": "jellyfin", "port": 8096},
    {"name": "longhorn-ui", "hostname": "longhorn", "port": 80},
    {"name": "loki-homelab", "port": 3100},
]


def test_netpol_callers_adds_homepage_for_a_widget_entry():
    entry = {"name": "radarr", "netpol_from": ["traefik"], "homepage_widget": True}
    assert netpol_callers(entry) == ["traefik", "homepage"]


def test_netpol_callers_leaves_an_unflagged_entry_alone():
    assert netpol_callers({"name": "radarr", "netpol_from": ["traefik"]}) == ["traefik"]
    assert netpol_callers({"name": "jellyfin"}) == []


def test_widget_url_names_the_entry_service_and_port():
    assert (
        homepage_widget_url(ENTRIES, "sonarr", "homelab")
        == "http://sonarr.homelab.svc.cluster.local:8989"
    )


def test_widget_url_refuses_an_entry_without_the_key():
    """The rejecting half: without the key the fence does not admit homepage."""
    with pytest.raises(ValueError, match="homepage_widget"):
        homepage_widget_url(ENTRIES, "jellyfin", "homelab")


def test_href_reads_the_hostname_and_takes_the_lan_name_and_a_path():
    assert (
        homepage_href(ENTRIES, "jellyfin", "example.com")
        == "https://jellyfin.example.com/"
    )
    assert (
        homepage_href(ENTRIES, "longhorn-ui", "example.com", lan=True)
        == "https://longhorn.local.example.com/"
    )
    assert (
        homepage_href(ENTRIES, "jellyfin", "example.com", "/web")
        == "https://jellyfin.example.com/web"
    )


def test_href_refuses_an_entry_with_no_route():
    with pytest.raises(ValueError, match="hostname"):
        homepage_href(ENTRIES, "loki-homelab", "example.com")
    with pytest.raises(ValueError, match="no containers_list entry"):
        homepage_href(ENTRIES, "nosuch", "example.com")


# A Service homepage dials whose fence a bespoke netpol-baseline template renders, so its entry
# cannot carry `homepage_widget` without rendering a second policy of the same name.
BESPOKE_FENCES = {"pihole": "networkpolicy-pihole.yaml.j2"}

# Targets the census must find. Named, because a reshaped URL drops out of a pattern silently.
KNOWN_TARGETS = frozenset(
    {"sonarr", "radarr", "qbittorrent", "scrutiny", "ical-proxy", "pihole"}
)

# http://<svc>[.<ns>[.svc[.cluster.local]]]:<port>
CLUSTER_URL = re.compile(
    r"^http://([a-z0-9-]+)(?:\.([a-z0-9-]+)(?:\.svc(?:\.cluster\.local)?)?)?:\d+(?:/|$)"
)


def in_namespace_targets(urls: set[str], ns: str) -> set[str]:
    """Service names the URLs dial in namespace `ns`, by FQDN or by bare name."""
    targets = set()
    for url in urls:
        m = CLUSTER_URL.match(url)
        if m and m.group(2) in (None, ns):
            targets.add(m.group(1))
    return targets


def unadmitted_targets(targets: set[str], entries: list[dict]) -> set[str]:
    """Targets neither carrying `homepage_widget` nor listed in BESPOKE_FENCES."""
    flagged = {e["name"] for e in entries if e.get("homepage_widget")}
    return targets - flagged - BESPOKE_FENCES.keys()


def test_every_in_namespace_widget_target_admits_homepage():
    targets = in_namespace_targets(config_urls(), namespace())
    assert unadmitted_targets(targets, containers_entries_in(host_context())) == set()
    assert KNOWN_TARGETS <= targets


def test_a_hand_written_url_to_an_unflagged_entry_is_flagged():
    """The rejecting half: the bypass the filter cannot see."""
    urls = {
        "http://jellyfin.homelab.svc.cluster.local:8096",
        "http://sonarr.homelab.svc.cluster.local:8989",
        "http://navidrome:4533/rest",
        "http://prometheus.observability.svc.cluster.local:9090/api/v1/query",
        "https://karakeep.local.example.com",
    }
    targets = in_namespace_targets(urls, "homelab")
    assert targets == {"jellyfin", "sonarr", "navidrome"}
    assert unadmitted_targets(targets, ENTRIES) == {"jellyfin", "navidrome"}


def test_each_bespoke_fence_admits_homepage():
    """An exemption holds only while the bespoke fence it names still admits homepage."""
    for name, template in BESPOKE_FENCES.items():
        docs = [
            doc
            for role, tpl, doc in rendered_docs()
            if tpl == template
            and doc.get("kind") == "NetworkPolicy"
            and doc["metadata"]["name"] == name
        ]
        assert docs, f"{template} no longer renders a NetworkPolicy named {name}"
        peers = [
            (peer.get("podSelector") or {}).get("matchLabels", {}).get("app")
            for doc in docs
            for rule in doc["spec"].get("ingress") or []
            for peer in rule.get("from") or []
        ]
        assert "homepage" in peers, f"{template} no longer admits homepage"
