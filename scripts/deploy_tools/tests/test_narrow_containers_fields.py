"""A `containers_list` entry edit reaches the readers of the fields it changed, not every reader.

Before #4340 any entry edit reached every role whose templates read the list. On the range of
PR #4310 six entry edits reached 23 readers that way, though most of them read one field the
edits never touched. `narrow_lib/containers.py` now maps an edit to its changed fields and a
reader to the fields its filters read, and keeps a reader only where the two meet. A reader it
cannot name the fields of (a loop, `selectattr`, a filter missing from `FILTER_FIELDS`) still
counts as reading every field.

`test_filter_fields_cover_every_key_each_filter_reads` holds `FILTER_FIELDS` against the filter
plugins' own source, so a filter that starts reading a new key fails here rather than
narrowing past a role whose render that key moves.

Run: uv run pytest scripts/deploy_tools/tests/test_narrow_containers_fields.py
"""

import ast
from pathlib import Path

import pytest

from deploy_tools.narrow_lib import containers
from lib.repo_paths import REPO

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


def test_a_removed_entry_changes_every_field():
    before = {"containers_list": [{"name": "a"}, {"name": "b"}]}
    assert (
        containers.changed_fields(before, {"containers_list": [{"name": "a"}]}) is None
    )


# ── FILTER_FIELDS against the filter plugins' source ────────────────────────────────────


def _filter_reads(source: str) -> dict[str, set[str]]:
    """Each registered filter's string keys, through the module functions it calls.

    A key is a constant in `x.get("k")`, `x["k"]` or `"k" in x`. That is a superset of the
    entry fields (a nested dict's keys come along), which errs toward more readers.
    """
    tree = ast.parse(source)
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    registered = {
        key.value: value.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Dict)
        for key, value in zip(node.keys, node.values, strict=True)
        if isinstance(key, ast.Constant)
        and isinstance(key.value, str)
        and isinstance(value, ast.Name)
    }
    reads: dict[str, set[str]] = {}
    for name, func in registered.items():
        keys: set[object] = set()
        seen: set[str] = set()
        todo = [func]
        while todo:
            fn = todo.pop()
            if fn in seen or fn not in funcs:
                continue
            seen.add(fn)
            for node in ast.walk(funcs[fn]):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    todo.append(node.func.id)
                elif (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                ):
                    keys.add(node.args[0].value)
                elif isinstance(node, ast.Subscript) and isinstance(
                    node.slice, ast.Constant
                ):
                    keys.add(node.slice.value)
                elif (
                    isinstance(node, ast.Compare)
                    and isinstance(node.left, ast.Constant)
                    and any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops)
                ):
                    keys.add(node.left.value)
        reads[name] = {k for k in keys if isinstance(k, str)}
    return reads


def _plugin_reads() -> dict[str, set[str]]:
    reads: dict[str, set[str]] = {}
    for path in sorted((REPO / "ansible" / "filter_plugins").glob("*.py")):
        reads.update(_filter_reads(path.read_text()))
    return reads


def test_filter_fields_cover_every_key_each_filter_reads():
    reads = _plugin_reads()
    # Non-vacuity: the scan must find the filters the table names, entry_port among them.
    assert "entry_port" in reads and set(containers.FILTER_FIELDS) <= set(reads)
    missing = {
        name: sorted(reads[name] - fields)
        for name, fields in containers.FILTER_FIELDS.items()
        if reads[name] - fields
    }
    assert not missing, f"FILTER_FIELDS omits keys these filters read: {missing}"


def test_the_scan_flags_a_filter_reading_a_key_the_table_omits():
    source = (
        "def entry_port(containers_list, name):\n"
        "    return _lookup(containers_list, name)['port']\n"
        "def _lookup(containers_list, name):\n"
        "    return [e for e in containers_list if e.get('hostname')][0]\n"
        "class FilterModule:\n"
        "    def filters(self):\n"
        "        return {'entry_port': entry_port}\n"
    )
    reads = _filter_reads(source)["entry_port"]
    assert reads - containers.FILTER_FIELDS["entry_port"] == {"hostname"}


def test_the_real_tree_names_filter_readers():
    """The narrowing finds its readers by pattern, so it must still see named ones."""
    reader = (
        REPO
        / "ansible/roles/k8s/uptime-kuma/templates/maintenance-sync-cronjob.yaml.j2"
    )
    assert containers.reader_fields(Path(reader).read_text()) == {"name", "port"}
