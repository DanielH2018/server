"""`cluster/services.yml` does not repeat a `name`.

The named-lookup filters (`entry_port`, `homepage_href`, `tier_priority_class`) return the
first entry of a repeated name, so a second copy would never render (#4399).

Run: uv run pytest tests/deploy/test_containers_list_names_unique.py
"""

from collections import Counter

from lib.render_guard import k8s_services


def _repeated(entries: list[dict]) -> list[str]:
    return sorted(
        n for n, count in Counter(e["name"] for e in entries).items() if count > 1
    )


def test_the_census_reads_the_service_list():
    """An empty read or a moved key would pass the uniqueness test below with nothing read."""
    assert {"traefik", "sonarr"} <= {e["name"] for e in k8s_services()}


def test_the_service_list_repeats_no_name():
    entries = k8s_services()
    assert not _repeated(entries), (
        f"cluster/services.yml repeats {_repeated(entries)}; the deploy narrowing keys "
        "entries by name, so an edit to the first copy would deploy nothing for it"
    )


def test_a_repeated_name_is_reported():
    entries = [{"name": "a", "port": 1}, {"name": "b"}, {"name": "a", "port": 2}]
    assert _repeated(entries) == ["a"]
