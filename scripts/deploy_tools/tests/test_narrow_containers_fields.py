"""A `containers_list` entry edit reaches the readers of the fields it changed, not every reader.

Before #4340 any entry edit reached every role whose templates read the list. On the range of
PR #4310 six entry edits reached 23 readers that way, though most of them read one field the
edits never touched. `narrow_lib/containers.py` now maps an edit to its changed fields and a
reader to the fields its filters read, and keeps a reader only where the two meet. A reader it
cannot name the fields of (a loop, `selectattr`, a filter missing from `FILTER_FIELDS`) still
counts as reading every field.

`test_filter_fields_cover_every_key_each_filter_reads` holds `FILTER_FIELDS` against the filter
plugins' own source, so a filter that starts reading a new key fails here rather than
narrowing past a role whose render that key moves. The scan in `_filter_field_scan.py` reports
any use of an entry it cannot resolve to a literal key as opaque: a computed key, iterating the
entry, handing it to a call or a method, returning it from the filter. An opaque use fails
`test_no_filter_in_the_table_reads_a_field_the_scan_cannot_name` unless `VETTED_OPAQUE` names
it with the reason it reads no field the row omits.

Run: uv run pytest scripts/deploy_tools/tests/test_narrow_containers_fields.py
"""

from pathlib import Path

import pytest

from deploy_tools.narrow_lib import containers
from lib.repo_paths import REPO

from _filter_field_scan import filter_reads
from _narrow_fixtures import HOST_VARS, Tree, _refs, build_tree

BOX = "ansible/inventory/host_vars/daniel-box.yml"


@pytest.fixture
def tree(tmp_path) -> Tree:
    t = build_tree(tmp_path)
    # prowlarr reads one entry's port, lidarr walks the whole list.
    t.write(
        "ansible/roles/k8s/prowlarr/templates/configmap.yaml.j2",
        "url: http://sonarr:{{ containers_list | entry_port('sonarr') }}\n",
    )
    t.write(
        "ansible/roles/k8s/lidarr/templates/configmap.yaml.j2",
        "{% for e in containers_list %}{{ e.name }}{% endfor %}\n",
    )
    t.commit("two readers of the list")
    return t


def test_an_edit_to_a_field_no_filter_reads_skips_the_filter_reader(tree: Tree):
    tree.write(BOX, HOST_VARS.replace("image: sonarr:1", "image: sonarr:2"))
    # sonarr is the entry's own tag; lidarr's loop reads every field; prowlarr reads `port`.
    assert tree.narrow(*_refs(tree)) == {"sonarr", "lidarr"}


def test_an_edit_to_a_field_the_filter_reads_reaches_the_filter_reader(tree: Tree):
    tree.write(
        BOX, HOST_VARS.replace("image: sonarr:1", "image: sonarr:1\n    port: 8989")
    )
    assert tree.narrow(*_refs(tree)) == {"sonarr", "lidarr", "prowlarr"}


def test_an_added_entry_reaches_every_reader(tree: Tree):
    tree.write(BOX, HOST_VARS + "  - name: readarr\n    platform: k8s\n")
    assert tree.narrow(*_refs(tree)) == {"readarr", "lidarr", "prowlarr"}


@pytest.mark.parametrize(
    "line",
    [
        "{{ containers_list | metrics_port('crowdsec') }}",
        "{{ hostvars['daniel-pi'].containers_list | entry_port('alloy') }}",
        "{{ (containers_list | homepage_href('docs', domain)) }}",
    ],
)
def test_a_filter_reader_has_the_filters_fields_is_clean(line: str):
    assert containers.reader_fields(line) is not None


@pytest.mark.parametrize(
    "line",
    [
        "{% for e in containers_list %}{{ e.port }}{% endfor %}",
        "{{ containers_list | selectattr('port', 'defined') | list }}",
        "{% for s in containers_list | default([]) | filter_by_platform('k8s') %}",
        "{{ containers_list }}",
        "{{ containers_list | entry_port('a') }} {{ containers_list[0].hostname }}",
    ],
    ids=["loop", "selectattr", "entries-returned", "bare", "one-bare-use-among-two"],
)
def test_a_reader_whose_fields_cannot_be_named_reads_every_field_is_flagged(line: str):
    assert containers.reader_fields(line) is None


def test_changed_fields_names_the_keys_an_edit_moved():
    before = {"containers_list": [{"name": "a", "port": 1, "image": "x"}]}
    after = {"containers_list": [{"name": "a", "port": 2, "image": "x", "tier": "t"}]}
    assert containers.changed_fields(before, after) == {"port", "tier"}


def test_a_reorder_reaches_every_reader(tree: Tree):
    """No field moves, but `scrape_jobs` and `kuma_ingress_monitors` render in list order."""
    radarr = "  - name: radarr\n    platform: k8s\n"
    tree.write(
        BOX,
        HOST_VARS.replace(radarr, "").replace(
            "containers_list:\n", "containers_list:\n" + radarr
        ),
    )
    assert tree.narrow(*_refs(tree)) == {"lidarr", "prowlarr"}


def test_a_reorder_changes_every_field():
    before = {"containers_list": [{"name": "a"}, {"name": "b"}]}
    after = {"containers_list": [{"name": "b"}, {"name": "a"}]}
    assert containers.changed_fields(before, after) is None


def test_a_removed_entry_changes_every_field():
    before = {"containers_list": [{"name": "a"}, {"name": "b"}]}
    assert (
        containers.changed_fields(before, {"containers_list": [{"name": "a"}]}) is None
    )


# ── FILTER_FIELDS against the filter plugins' source ────────────────────────────────────


# A use the scan cannot resolve to a literal field name, vetted as reading no field the row omits.
VETTED_OPAQUE = {
    ("in_service_tier", "found.append()"): (
        "`tier_entries` collects whole entries into the list it returns, and "
        "`in_service_tier` reads each one by the literal key `name`"
    ),
}


def _plugin_reads() -> dict[str, tuple[set[str], set[str]]]:
    reads: dict[str, tuple[set[str], set[str]]] = {}
    for path in sorted((REPO / "ansible" / "filter_plugins").glob("*.py")):
        reads.update(filter_reads(path.read_text()))
    return reads


def _unvetted(name: str, opaque: set[str]) -> set[str]:
    return {use for use in opaque if (name, use) not in VETTED_OPAQUE}


def test_filter_fields_cover_every_key_each_filter_reads():
    reads = _plugin_reads()
    # Non-vacuity: the scan must find the filters the table names, entry_port among them.
    assert "entry_port" in reads and set(containers.FILTER_FIELDS) <= set(reads)
    missing = {
        name: sorted(reads[name][0] - fields)
        for name, fields in containers.FILTER_FIELDS.items()
        if reads[name][0] - fields
    }
    assert not missing, f"FILTER_FIELDS omits keys these filters read: {missing}"


def test_no_filter_in_the_table_reads_a_field_the_scan_cannot_name():
    """A row whose filter iterates or forwards the entry would narrow past a moved render."""
    reads = _plugin_reads()
    # Non-vacuity: the table leaves `filter_by_platform` out because it returns whole entries,
    # and the scan must reach the same verdict from the source.
    assert any(use.startswith("return ") for use in reads["filter_by_platform"][1])
    opaque = {
        name: sorted(_unvetted(name, reads[name][1]))
        for name in containers.FILTER_FIELDS
        if _unvetted(name, reads[name][1])
    }
    assert not opaque, (
        "these filters read fields no constant names; drop their FILTER_FIELDS row so their "
        f"readers read every field, or vet the use in VETTED_OPAQUE: {opaque}"
    )
    # The vetted uses must still exist, or the exemption outlived its subject.
    assert all(use in reads[name][1] for name, use in VETTED_OPAQUE)


_ENTRY_PORT = (
    "def entry_port(containers_list, name):\n"
    "    return _lookup(containers_list, name)['port']\n"
    "def _lookup(containers_list, name):\n"
    "    return [e for e in containers_list if {probe}][0]\n"
    "class FilterModule:\n"
    "    def filters(self):\n"
    "        return {{'entry_port': entry_port}}\n"
)


def test_the_scan_flags_a_filter_reading_a_key_the_table_omits():
    keys, _ = filter_reads(_ENTRY_PORT.format(probe="e.get('hostname')"))["entry_port"]
    assert keys - containers.FILTER_FIELDS["entry_port"] == {"hostname"}


@pytest.mark.parametrize(
    ("probe", "use"),
    [
        ("any(e.values())", ".values()"),
        ("e.get(name)", ".get(<computed>)"),
        ("render(**e)", "**"),
        ("getattr(e, name)", "getattr()"),
        ("e[name]", "e[name]"),
        ("any(k for k in e)", "for over e"),
        ("json.dumps(e)", "json.dumps()"),
        ("dict(e)", "dict()"),
    ],
)
def test_the_scan_flags_a_filter_that_iterates_or_forwards_the_entry(probe, use):
    _, opaque = filter_reads(_ENTRY_PORT.format(probe=probe))["entry_port"]
    assert use in opaque


def test_the_real_tree_names_filter_readers():
    """The narrowing finds its readers by pattern, so it must still see named ones."""
    reader = (
        REPO
        / "ansible/roles/k8s/uptime-kuma/templates/maintenance-sync-cronjob.yaml.j2"
    )
    assert containers.reader_fields(Path(reader).read_text()) == {"name", "port"}


# The entry reaches a helper as a parameter here, and the helper has a local of its own. That is
# the shape the comprehension harness above cannot exercise: there `e` is a loop variable.
_HELPER = (
    "def entry_port(containers_list, name):\n"
    "    for e in containers_list:\n"
    "        if e.get('name') == name:\n"
    "            return _read(e, name)\n"
    "def _read(entry, key):\n"
    "    out = {{}}\n"
    "    {probe}\n"
    "    return out['port']\n"
    "class FilterModule:\n"
    "    def filters(self):\n"
    "        return {{'entry_port': entry_port}}\n"
)


@pytest.mark.parametrize(
    ("probe", "use"),
    [
        ("out['port'] = entry[key]", "entry[key]"),
        ("for k in entry: out[k] = 1", "for over entry"),
        ("out = dict(entry)", "dict()"),
        ("out.update(entry)", "out.update()"),
        ("out['port'] = ', '.join(entry)", "', '.join()"),
    ],
    ids=["computed-key", "iterates", "walks", "attribute-call", "constant-receiver"],
)
def test_a_helper_reading_an_entry_it_was_handed_by_a_computed_route_is_flagged(
    probe, use
):
    _, opaque = filter_reads(_HELPER.format(probe=probe))["entry_port"]
    assert use in opaque


@pytest.mark.parametrize(
    "probe",
    ["out['port'] = entry['port']", "out['port'] = entry.get('port', 0)", "pass"],
)
def test_a_helper_reading_an_entry_by_literal_keys_is_clean(probe):
    _, opaque = filter_reads(_HELPER.format(probe=probe))["entry_port"]
    assert opaque == set()


def test_the_comprehension_harness_reading_a_literal_key_is_clean():
    """Without this the probes above could pass because the harness flags everything."""
    _, opaque = filter_reads(_ENTRY_PORT.format(probe="e.get('name') == name"))[
        "entry_port"
    ]
    assert opaque == set()


def test_a_filter_returning_a_whole_entry_is_flagged():
    """The template then reads whatever it likes from it, as from `selectattr`."""
    source = _HELPER.replace("return _read(e, name)", "return e")
    _, opaque = filter_reads(source.format(probe="pass"))["entry_port"]
    assert "return e" in opaque
