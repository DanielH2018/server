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
    ],
)
def test_a_malformed_list_is_refused(raw, message):
    with pytest.raises(ContainerEntryError, match=message):
        parse_containers_list(raw)
