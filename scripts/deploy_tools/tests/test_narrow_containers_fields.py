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
import builtins
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


_BUILTINS = frozenset(dir(builtins)) - {"getattr", "vars"}
_ITERATES = frozenset({"items", "keys", "values"})

# A use the key scan cannot follow, vetted as reading no entry field the row omits.
VETTED_OPAQUE = {
    (
        "metrics_port",
        ".values()",
    ): "iterates a `metrics` item's `params`, under `metrics`",
    (
        "scrape_jobs",
        ".values()",
    ): "iterates a `metrics` item's `params`, under `metrics`",
}


def _filter_reads(source: str) -> dict[str, tuple[set[str], set[str]]]:
    """Each registered filter's string keys and its opaque uses, through the functions it calls.

    A key is a constant in `x.get("k")`, `x["k"]` or `"k" in x`. That is a superset of the
    entry fields (a nested dict's keys come along), which errs toward more readers. An opaque
    use is one that could read a field no constant names: iterating a mapping, `**`,
    `getattr`, a `.get` with a computed key, or a call to a function outside the module.
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
        and value.id in funcs
    }
    reads: dict[str, tuple[set[str], set[str]]] = {}
    for name, func in registered.items():
        keys: set[object] = set()
        opaque: set[str] = set()
        seen: set[str] = set()
        todo = [func]
        while todo:
            fn = todo.pop()
            if fn in seen:
                continue
            seen.add(fn)
            for node in ast.walk(funcs[fn]):
                keys |= _constant_keys(node)
                opaque |= _opaque_uses(node, funcs)
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id in funcs
                ):
                    todo.append(node.func.id)
        reads[name] = ({k for k in keys if isinstance(k, str)}, opaque)
    return reads


def _constant_keys(node: ast.AST) -> set[object]:
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    ):
        return {node.args[0].value}
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
        return {node.slice.value}
    if (
        isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Constant)
        and any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops)
    ):
        return {node.left.value}
    return set()


def _opaque_uses(node: ast.AST, funcs: dict[str, ast.FunctionDef]) -> set[str]:
    if isinstance(node, ast.Dict) and None in node.keys:
        return {"**"}
    if not isinstance(node, ast.Call):
        return set()
    if any(kw.arg is None for kw in node.keywords):
        return {"**"}
    if isinstance(node.func, ast.Name):
        callee = node.func.id
        if callee in funcs or callee in _BUILTINS or callee.endswith("Error"):
            return set()
        return {f"{callee}()"}
    if isinstance(node.func, ast.Attribute):
        if node.func.attr in _ITERATES:
            return {f".{node.func.attr}()"}
        if node.func.attr == "get" and not (
            node.args and isinstance(node.args[0], ast.Constant)
        ):
            return {".get(<computed>)"}
    return set()


def _plugin_reads() -> dict[str, tuple[set[str], set[str]]]:
    reads: dict[str, tuple[set[str], set[str]]] = {}
    for path in sorted((REPO / "ansible" / "filter_plugins").glob("*.py")):
        reads.update(_filter_reads(path.read_text()))
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
    keys, _ = _filter_reads(_ENTRY_PORT.format(probe="e.get('hostname')"))["entry_port"]
    assert keys - containers.FILTER_FIELDS["entry_port"] == {"hostname"}


@pytest.mark.parametrize(
    ("probe", "use"),
    [
        ("any(e.values())", ".values()"),
        ("e.get(name)", ".get(<computed>)"),
        ("render(**e)", "**"),
        ("getattr(e, name)", "getattr()"),
    ],
)
def test_the_scan_flags_a_filter_that_iterates_or_forwards_the_entry(probe, use):
    _, opaque = _filter_reads(_ENTRY_PORT.format(probe=probe))["entry_port"]
    assert use in opaque


def test_the_real_tree_names_filter_readers():
    """The narrowing finds its readers by pattern, so it must still see named ones."""
    reader = (
        REPO
        / "ansible/roles/k8s/uptime-kuma/templates/maintenance-sync-cronjob.yaml.j2"
    )
    assert containers.reader_fields(Path(reader).read_text()) == {"name", "port"}
