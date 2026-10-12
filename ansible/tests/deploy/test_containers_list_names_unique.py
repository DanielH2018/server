"""No host's `containers_list` repeats a `name`.

The deploy-plane narrowing assumes each name is unique. `narrow_lib/containers.py:entries` keys
the list by name and keeps the last entry of a repeated name, while the named-lookup filters
(`entry_port`, `homepage_href`, `tier_priority_class`) return the first. An edit to the first
copy would then tag no role for that entry (#4399). CI keeps a duplicate off master, and the
deployer narrows only CI-green master, so the narrowing never meets one.

Run: uv run pytest ansible/tests/deploy/test_containers_list_names_unique.py
"""

from collections import Counter

import pytest
from lib import yaml_fast
from lib.ansible_inventory import containers_entries_in

from _helpers import HOST_VARS


def _lists() -> dict[str, list[dict]]:
    """Each host_vars file's named entries, the `_example.yml` template included."""
    return {
        path.stem: containers_entries_in(yaml_fast.safe_load(path.read_text()))
        for path in sorted(HOST_VARS.glob("*.yml"))
    }


def _repeated(entries: list[dict]) -> list[str]:
    return sorted(
        n for n, count in Counter(e["name"] for e in entries).items() if count > 1
    )


def test_the_census_reads_both_deploying_hosts():
    """An empty glob or a moved key would pass the uniqueness test below with nothing read."""
    lists = _lists()
    assert lists.get("daniel-box") and lists.get("daniel-pi")


@pytest.mark.parametrize(("host", "entries"), sorted(_lists().items()))
def test_no_host_repeats_a_containers_list_name(host: str, entries: list[dict]):
    assert not _repeated(entries), (
        f"{host}'s containers_list repeats {_repeated(entries)}; the deploy narrowing keys "
        "entries by name, so an edit to the first copy would deploy nothing for it"
    )


def test_a_repeated_name_is_reported():
    entries = [{"name": "a", "port": 1}, {"name": "b"}, {"name": "a", "port": 2}]
    assert _repeated(entries) == ["a"]
