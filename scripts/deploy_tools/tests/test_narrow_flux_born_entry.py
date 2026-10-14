"""An added or removed Flux-born `containers_list` entry reaches only the readers it can move.

D2.1c of `docs/target-architecture/deploy/d2-first-leaf.md`. Before it, any added entry made
`changed_fields` return None, so a Flux-born entry reached all 23 reader tags. The entry is now
recorded by the fields it carries, so the tree below narrows it to its own tag, the five
readers of every field, and the `entry_names` reader, which lists every entry by name. A
reader of `port`, `tier`, `metrics` or `hostname` is kept only when the entry carries the field.

`ansible/tests/k8s/test_flux_born_entry_reader_census.py` holds the same rule against the
real tree's renders.

Run: uv run pytest scripts/deploy_tools/tests/test_narrow_flux_born_entry.py
"""

import pytest

from deploy_tools.narrow_lib import broad as narrow_broad
from deploy_tools.narrow_lib import containers

from _narrow_fixtures import (
    DECLARED,
    HOST_VARS,
    WHOLE_ENTRY_READERS,
    Tree,
    _refs,
    build_tree,
    write_whole_entry_readers,
)

BOX = "ansible/inventory/host_vars/daniel-box.yml"
BARE = "  - name: bento-pdf\n    platform: k8s\n    owner: flux\n"
# One reader per field a bare entry does not carry, each through a filter whose row names it,
# and radarr's `entry_names`, whose output lists every entry whatever it carries.
FIELD_READERS = {
    "prowlarr": "{{ containers_list | published_ports | join(',') }}\n",
    "lidarr": "{{ containers_list | scrape_jobs('media') | to_json }}\n",
    "bazarr": "{{ containers_list | in_service_tier(peer, 'platform') }}\n",
    "jellyfin": "{{ containers_list | kuma_ingress_monitors | to_json }}\n",
    "radarr": "{{ containers_list | entry_names | join(',') }}\n",
}
EVERY_ENTRY_READERS = set(WHOLE_ENTRY_READERS) | {"radarr"}


def _narrow(tmp_path, before: str, after: str) -> tuple[set[str], list[str]]:
    """Commit the readers and the `before` list, then `after`; narrow it and keep the journal."""
    t: Tree = build_tree(tmp_path)
    write_whole_entry_readers(t)
    for role, text in FIELD_READERS.items():
        t.write(f"ansible/roles/k8s/{role}/templates/reader.yaml.j2", text)
    t.write(BOX, before)
    t.commit("five readers of every field, one of each named field")
    t.write(BOX, after)
    # Bystander tags, as on the real fleet's 59, so a narrowing under half the fleet passes.
    declared = DECLARED | set(WHOLE_ENTRY_READERS) | {"bento-pdf"}
    declared |= {f"other-{n}" for n in range(8)}
    lines: list[str] = []
    tags = narrow_broad.narrow(
        *_refs(t), cwd=t.root, declared=declared, callers={}, explain=lines.append
    )
    return tags, lines


def test_an_added_flux_born_entry_reaches_its_tag_and_the_every_entry_readers_is_clean(
    tmp_path,
):
    tags, lines = _narrow(tmp_path, HOST_VARS, HOST_VARS + BARE)
    assert tags == {"bento-pdf"} | EVERY_ENTRY_READERS
    assert (
        "narrow: containers_list/bento-pdf added with owner flux, counting platform and"
        " its presence"
    ) in lines


def test_a_removed_flux_born_entry_reaches_the_every_entry_readers_alone_is_clean(
    tmp_path,
):
    tags, _ = _narrow(tmp_path, HOST_VARS + BARE, HOST_VARS)
    assert tags == EVERY_ENTRY_READERS


def test_an_added_flux_born_entry_with_hostname_reaches_its_readers_is_flagged(
    tmp_path,
):
    """The keep half: a field the entry carries reaches that field's readers."""
    tags, _ = _narrow(tmp_path, HOST_VARS, HOST_VARS + BARE + "    hostname: pdf\n")
    assert tags == {"bento-pdf", "jellyfin"} | EVERY_ENTRY_READERS


def _doc(*entries: dict) -> dict:
    return {"containers_list": list(entries)}


SONARR, RADARR = {"name": "sonarr", "port": 1}, {"name": "radarr"}


@pytest.mark.parametrize(
    "owner",
    [{}, {"owner": "bogus"}, {"owner": ["flux"]}],
    ids=["ansible-owned", "unknown-owner", "unhashable-owner"],
)
def test_an_added_entry_not_born_of_flux_changes_every_field_is_flagged(owner: dict):
    added = {"name": "bento-pdf", "platform": "k8s", **owner}
    before = _doc(SONARR, RADARR)
    assert containers.changed_fields(before, _doc(SONARR, RADARR, added)) is None


def test_a_flux_born_entry_added_beside_a_swap_changes_every_field_is_flagged():
    added = {"name": "bento-pdf", "owner": "flux"}
    before = _doc(SONARR, RADARR)
    assert containers.changed_fields(before, _doc(RADARR, SONARR, added)) is None


def test_a_bare_flux_born_entry_is_recorded_with_its_presence_alone_is_clean():
    """Even an entry carrying no field but `name` and `owner` is recorded, under its name."""
    added = {"name": "bento-pdf", "owner": "flux"}
    before = _doc(SONARR, RADARR)
    assert containers.changed_fields(before, _doc(SONARR, added, RADARR)) == {
        "bento-pdf": {containers.PRESENCE}
    }
