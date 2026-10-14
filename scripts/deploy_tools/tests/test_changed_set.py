"""`changed_set`: which Kustomizations a bundle changes, from two `digests.json` files.

Each fixture is a `digests.json` in contract C1's format: a name to its `own` and `footprint`
digests, plus `_unclassified`.

Run: uv run pytest scripts/deploy_tools/tests/test_changed_set.py
"""

from changed_set import UNCLASSIFIED, changed_set

GRAPH = {"homelab": [], "bento-pdf": [], "sonarr": [], "cluster-vars": []}


def digests(**footprints: str) -> dict:
    """A `digests.json` holding each name's footprint, `own` beside it as C1 writes it."""
    return {
        name.replace("_", "-") if name != UNCLASSIFIED else name: {
            "own": f"own-{value}",
            "footprint": value,
        }
        for name, value in footprints.items()
    }


OLD = digests(bento_pdf="a", sonarr="s", cluster_vars="v", _unclassified="u")


def test_a_footprint_that_differs_for_one_tag_changes_that_tag_alone():
    new = digests(bento_pdf="b", sonarr="s", cluster_vars="v", _unclassified="u")
    result = changed_set(new, OLD, GRAPH)
    assert result.names == {"bento-pdf"} and not result.widened


def test_equal_footprints_change_nothing():
    assert changed_set(OLD, OLD, GRAPH).names == frozenset()


def test_an_unclassified_change_counts_as_every_name():
    new = digests(bento_pdf="a", sonarr="s", cluster_vars="v", _unclassified="u2")
    result = changed_set(new, OLD, GRAPH)
    assert result.names == {"bento-pdf", "sonarr", "cluster-vars"}
    assert "_unclassified" in result.widened


def test_an_ancestor_with_no_digests_json_counts_as_every_name():
    result = changed_set(OLD, None, GRAPH)
    assert result.names == {"bento-pdf", "sonarr", "cluster-vars"}
    assert result.widened == "the older digests.json could not be read"


def test_no_newer_digests_json_counts_as_every_listed_kustomization():
    result = changed_set(None, OLD, {**GRAPH, "radarr": []})
    assert result.names == {"bento-pdf", "sonarr", "cluster-vars", "radarr"}
    assert result.widened


def test_a_malformed_file_counts_as_every_name():
    result = changed_set(OLD, {"sonarr": {"own": "x"}}, GRAPH)
    assert result.names == {"bento-pdf", "sonarr", "cluster-vars"}
    assert result.widened == "the older digests.json is malformed"


def test_a_dependent_of_a_changed_name_is_in_the_set():
    graph = {**GRAPH, "bento-pdf": ["cluster-vars"], "sonarr": ["bento-pdf"]}
    new = digests(bento_pdf="a", sonarr="s", cluster_vars="v2", _unclassified="u")
    assert changed_set(new, OLD, graph).names == {"cluster-vars", "bento-pdf", "sonarr"}


def test_an_added_name_is_in_the_set_and_a_removed_one_is_not():
    new = digests(bento_pdf="a", radarr="r", cluster_vars="v", _unclassified="u")
    assert changed_set(new, OLD, GRAPH).names == {"radarr"}


def test_a_suspended_kustomization_is_left_out():
    new = digests(bento_pdf="b", sonarr="s2", cluster_vars="v", _unclassified="u")
    assert changed_set(new, OLD, GRAPH, suspended={"sonarr"}).names == {"bento-pdf"}
