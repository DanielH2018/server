"""`parse_containers_list`, the one check of `containers_list`'s entry shape.

Run: uv run pytest scripts/lib/tests/script_census/test_container_entry.py
"""

import pytest
import yaml

from lib.container_entry import (
    ContainerEntry,
    ContainerEntryError,
    parse_containers_list,
)


def test_entries_come_back_in_list_order_as_container_entries():
    entries = parse_containers_list(
        [{"name": "b", "tier": "platform"}, {"name": "a", "port": 1}]
    )
    assert entries == ({"name": "b", "tier": "platform"}, {"name": "a", "port": 1})
    assert [type(e) for e in entries] == [ContainerEntry, ContainerEntry]
    assert [e.name for e in entries] == ["b", "a"]
    assert parse_containers_list(None) == ()


def test_an_entry_dumps_to_yaml_as_a_plain_mapping():
    [entry] = parse_containers_list([{"name": "a", "port": 1}])
    assert yaml.safe_dump(entry) == "name: a\nport: 1\n"
    assert yaml.dump(entry, Dumper=yaml.CSafeDumper) == "name: a\nport: 1\n"


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"name": "a"}, "must be a list"),
        ([{"name": "a"}, "not-a-mapping"], "item 1 must be a mapping"),
        ([{"port": 1}], "item 0 has no `name`"),
        ([{"name": ""}], "item 0 has no `name`"),
        ([{"name": 7}], "item 0 has no `name`"),
        ([{"name": "a"}, {"name": "a"}], "names 'a' twice"),
        ([{"name": "a", "tier": "home-edg"}], "unknown tier 'home-edg'"),
        ([{"name": "a", "tier": "home-critical"}], "unknown tier 'home-critical'"),
        (
            [{"name": "a", "use_autelia": True}],
            "entry 'a' has unknown key 'use_autelia'",
        ),
        (
            [{"name": "a", "networks": ["proxy"]}],
            "entry 'a' has unknown key 'networks'",
        ),
        ([{"name": "a", "port": "80"}], "entry 'a' key 'port'"),
    ],
    ids=[
        "not-a-list",
        "non-mapping",
        "no-name",
        "empty-name",
        "non-string-name",
        "duplicate",
        "misspelt-tier",
        "group-as-tier-has-no-single-priority-class",
        "misspelt-key",
        "compose-only-key",
        "port-as-string",
    ],
)
def test_a_malformed_list_is_refused(raw, message):
    with pytest.raises(ContainerEntryError, match=message):
        parse_containers_list(raw)


def test_compose_services_takes_its_own_keys_and_refuses_a_k8s_one():
    [entry] = parse_containers_list(
        [{"name": "alloy", "networks": ["proxy"], "port": 1}], key="compose_services"
    )
    assert entry == {"name": "alloy", "networks": ["proxy"], "port": 1}
    with pytest.raises(ContainerEntryError, match="has unknown key 'hostname'"):
        parse_containers_list(
            [{"name": "alloy", "hostname": "x"}], key="compose_services"
        )
