"""Unit tests for filter_plugins/homepage_tiles.py.

`homepage_widget: true` on a containers_list entry both opens the entry's fence to homepage
(`netpol_callers`) and is the only way homepage's tile list builds a ClusterIP widget URL
(`homepage_widget_url`). Refusing an unflagged entry is what keeps the two halves equal (#3691).
That the fences actually render the derived callers is test_netpol_from.py's job.

Run: uv run pytest ansible/tests/services/test_homepage_tiles_filter.py
"""

import pytest

from homepage_tiles import homepage_href, homepage_widget_url, netpol_callers

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
