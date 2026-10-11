"""Unit tests for filter_plugins/entry_fields.py.

Each filter replaced a Jinja loop over `containers_list` (#4395), and the five templates it
serves render byte-identical through it. These pin the two rules a loop spelled inline:
which entries contribute, and in what order.

Run: uv run pytest ansible/tests/deploy/test_entry_fields_filter.py
"""

from entry_fields import entry_names, lan_hostnames, published_ports

ENTRIES = [
    {"name": "wg-easy", "port": 51821, "platform": "k8s", "hostname": "wg"},
    {"name": "autoheal"},
    {
        "name": "obs",
        "platform": "k8s",
        "hostname": "grafana",
        "extra_hostnames": ["prom"],
    },
    {"name": "dns", "platform": "k8s", "extra_hostnames": ["dns-alias"]},
    {"name": "glances", "port": 61208, "hostname": "glances"},
    "not-an-entry",
]


def test_entry_names_lists_every_entry_in_order():
    assert entry_names(ENTRIES) == ["wg-easy", "autoheal", "obs", "dns", "glances"]


def test_published_ports_pairs_each_entry_that_sets_a_port():
    assert published_ports(ENTRIES) == ["wg-easy:51821", "glances:61208"]


def test_lan_hostnames_takes_each_hostname_then_its_extras():
    assert lan_hostnames(ENTRIES) == ["wg", "grafana", "prom", "dns-alias", "glances"]


def test_lan_hostnames_keeps_only_the_platform_asked_for():
    """`glances` sets no platform, which reads as docker, as `filter_by_platform` reads it."""
    assert lan_hostnames(ENTRIES, "k8s") == ["wg", "grafana", "prom", "dns-alias"]
    assert lan_hostnames(ENTRIES, "docker") == ["glances"]
