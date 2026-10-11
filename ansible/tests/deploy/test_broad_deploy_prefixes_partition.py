"""The deploy plane splits into the play half and the census half, and into nothing else.

`deploy_changes._BROAD_DEPLOY_PREFIXES` is every path the deployer routes to
`ansible/deploy.yml` rather than to a service. Two consumers divide it: `narrow_broad`'s
`CENSUS_PREFIXES` are the trees a per-path consumer rule can trace to the roles that read
them, and `PLAY_PREFIXES` are the rest — paths every play reads, which `broad_path_tags`
refuses outright.

`PLAY_PREFIXES` is derived as the subtraction, so a prefix added to the deployer's broad set
cannot land in neither half. A deploy-plane path in neither half would read as narrowable by a
rule that has none, which is the silent direction. This is the guard that the derivation stays
a partition.

Run: uv run pytest ansible/tests/deploy/test_broad_deploy_prefixes_partition.py
"""

from deploy_logic import _BROAD_DEPLOY_PREFIXES
from deploy_tools.narrow_lib import broad as narrow_broad


def test_the_two_halves_partition_the_deploy_plane_exactly():
    """Checked as a partition rather than as a subset: an overlap would hand
    `broad_path_tags` a path it refuses outright AND counts in the staleness census."""
    play = frozenset(narrow_broad.PLAY_PREFIXES)
    census = frozenset(narrow_broad.CENSUS_PREFIXES)
    assert play | census == frozenset(_BROAD_DEPLOY_PREFIXES)
    assert not play & census


def test_each_half_holds_a_named_member():
    """Non-vacuity: an empty tuple on either side would pass the union above as an `|` over
    nothing, so each half names a prefix it must hold."""
    assert "ansible/deploy.yml" in narrow_broad.PLAY_PREFIXES
    assert "ansible/inventory/" in narrow_broad.CENSUS_PREFIXES
