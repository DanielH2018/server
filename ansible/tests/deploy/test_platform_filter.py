#!/usr/bin/env python3
"""Unit tests for filter_by_platform in filter_plugins/toposort.py.

This filter decides which containers a deploy touches at all. Its default is
load-bearing: all 46 existing containers_list entries omit `platform`, so a
default of anything but "docker" would silently skip every service on the next
deploy. The default-behaviour tests below are the guard against that.

Lives in ansible/tests/ (not under filter_plugins/) so Ansible's filter-plugin
loader doesn't import it as a plugin; the `pythonpath` setting in pyproject.toml
puts filter_plugins/ on sys.path so `import toposort` resolves.

Run: uv run pytest ansible/tests/deploy/test_platform_filter.py
"""

from toposort import filter_by_platform


def _c(name, platform=None):
    entry = {"name": name}
    if platform is not None:
        entry["platform"] = platform
    return entry


def _names(containers):
    return [c["name"] for c in containers]


def test_entries_without_platform_key_default_to_docker():
    # The critical case: every existing containers_list entry looks like this.
    containers = [_c("traefik"), _c("authelia"), _c("jellyfin")]
    assert _names(filter_by_platform(containers, "docker")) == [
        "traefik",
        "authelia",
        "jellyfin",
    ]


def test_entries_without_platform_key_are_excluded_from_k8s():
    containers = [_c("traefik"), _c("authelia")]
    assert filter_by_platform(containers, "k8s") == []


def test_explicit_platform_selects_matching_entries():
    containers = [_c("traefik"), _c("speedtest", "k8s"), _c("jellyfin", "docker")]
    assert _names(filter_by_platform(containers, "docker")) == ["traefik", "jellyfin"]
    assert _names(filter_by_platform(containers, "k8s")) == ["speedtest"]


def test_platform_defaults_to_docker_when_argument_omitted():
    containers = [_c("traefik"), _c("speedtest", "k8s")]
    assert _names(filter_by_platform(containers)) == ["traefik"]


def test_input_order_is_preserved():
    containers = [_c("z"), _c("a"), _c("m")]
    assert _names(filter_by_platform(containers, "docker")) == ["z", "a", "m"]


def test_empty_list_returns_empty_list():
    assert filter_by_platform([], "docker") == []


def test_original_list_is_not_mutated():
    containers = [_c("traefik"), _c("speedtest", "k8s")]
    filter_by_platform(containers, "docker")
    assert _names(containers) == ["traefik", "speedtest"]
