"""An inventory key a range moved reaches the setup roles that template it (#6962).

PR #6952 moved `host_python_version` in `group_vars/all.yml`. Eight setup roles render it into
their units, and the tick routed the key to the deploy plane alone. The fixture is
`_setup_role_fixtures.build`'s `demo` role, whose `alpha` template reads the key, beside an
inventory and a play.
"""

import json

import pytest

import secret_setup_readers
from deploy_tools.land_lib.key_readers import KeyReader, key_readers
from deploy_tools.narrow_lib import broad, inventory_keys

from _narrow_fixtures import Tree, _refs
from _setup_role_fixtures import ROLE, build, narrow

GROUP_VARS = "ansible/inventory/group_vars/all.yml"
SECRETS = "ansible/vars/secrets.yml"
BASE_VARS = """\
# host_python_version is documented here, and the comment reads nothing.
host_python_version: "3.14.6"
uv_pin: "cpython-{{ host_python_version }}"
play_key: read-by-the-play
"""


@pytest.fixture
def tree(tmp_path) -> Tree:
    t = build(tmp_path)
    t.write(GROUP_VARS, BASE_VARS)
    # The comment names the key in prose, as the real file names `server_ip`.
    t.write(
        "ansible/inventory/hosts.ini",
        "# host_python_version is set in group_vars.\n[all]\ndaniel-box\n",
    )
    t.write("ansible/deploy.yml", "- hosts: all\n  vars:\n    p: '{{ play_key }}'\n")
    t.write(f"{ROLE}/templates/alpha.conf.j2", "py = {{ host_python_version }}\n")
    # Copied verbatim by a task, so the key named here is never rendered.
    t.write(f"{ROLE}/files/notes.py", '"""Runs under play_key and uv_pin."""\n')
    t.commit("inventory")
    return t


def _moved(tree: Tree, old: str, new: str) -> tuple[str, str]:
    tree.write(GROUP_VARS, BASE_VARS.replace(old, new))
    return _refs(tree)


def _setup_readers(tree: Tree, capsys, refs: tuple[str, str]) -> tuple[int, dict]:
    """`secret_setup_readers.py` over `refs`, as the tick runs it."""
    rc = secret_setup_readers.main([*refs, "--repo", str(tree.root)])
    out = capsys.readouterr().out.strip()
    return rc, json.loads(out) if out else {}


def test_a_moved_key_a_setup_template_reads_names_that_template(tree, capsys):
    refs = _moved(tree, '"3.14.6"', '"3.15.2"')
    assert _setup_readers(tree, capsys, refs) == (
        0,
        {"paths": [f"{ROLE}/templates/alpha.conf.j2"], "unread": {}, "refused": ""},
    )


def test_a_key_only_the_play_and_files_name_adds_no_setup_file(tree, capsys):
    """`deploy.yml` reading it refuses the deploy plane, and says nothing about a setup role.

    It moves beside a key a template reads, so the answer is that template alone rather than
    a refusal or the verbatim `files/notes.py`.
    """
    tree.write(
        GROUP_VARS,
        BASE_VARS.replace('"3.14.6"', '"3.15.2"').replace("read-by-the-play", "moved"),
    )
    assert _setup_readers(tree, capsys, _refs(tree)) == (
        0,
        {"paths": [f"{ROLE}/templates/alpha.conf.j2"], "unread": {}, "refused": ""},
    )


def test_a_value_built_from_the_moved_key_reaches_its_readers(tree, capsys):
    tree.write(f"{ROLE}/templates/beta.conf.j2", "uv = {{ uv_pin }}\n")
    tree.commit("beta reads the derived pin")
    refs = _moved(tree, '"3.14.6"', '"3.15.2"')
    assert _setup_readers(tree, capsys, refs)[1]["paths"] == [
        f"{ROLE}/templates/alpha.conf.j2",
        f"{ROLE}/templates/beta.conf.j2",
    ]


def _macro_carries_the_key(tree: Tree) -> None:
    tree.write("ansible/templates/python.j2", "{{ host_python_version }}\n")
    tree.write(f"{ROLE}/templates/beta.conf.j2", "{% include 'python.j2' %}\n")
    tree.commit("a macro carries the key")


def test_a_shared_template_a_setup_role_includes_routes_the_including_file(
    tree, capsys
):
    """A refusal here routed nothing and recorded nothing, the silence #6962 reports."""
    _macro_carries_the_key(tree)
    refs = _moved(tree, '"3.14.6"', '"3.15.2"')
    assert _setup_readers(tree, capsys, refs)[1]["paths"] == [
        f"{ROLE}/templates/alpha.conf.j2",
        f"{ROLE}/templates/beta.conf.j2",
    ]


def test_role_tags_narrows_a_key_a_shared_template_carries_to_its_includer(tree):
    _macro_carries_the_key(tree)
    got = narrow(tree, *_moved(tree, '"3.14.6"', '"3.15.2"'))
    assert got == frozenset({"alpha", "beta"})


def test_a_hosts_ini_inline_variable_built_from_the_key_reaches_its_readers(
    tree, capsys
):
    tree.write(
        "ansible/inventory/hosts.ini",
        "[all]\ndaniel-box box_py=v{{ host_python_version }}\n",
    )
    tree.write(f"{ROLE}/templates/beta.conf.j2", "py = {{ box_py }}\n")
    tree.commit("an inline variable carries the key")
    refs = _moved(tree, '"3.14.6"', '"3.15.2"')
    assert (
        f"{ROLE}/templates/beta.conf.j2"
        in _setup_readers(tree, capsys, refs)[1]["paths"]
    )


def test_a_secrets_refusal_keeps_the_inventory_readers_of_the_same_range(tree, capsys):
    """The secrets half refuses a key a play reads; the inventory half's answer stands."""
    secrets = "s_key: ENC[AES256_GCM,data:aaa]\nsops:\n    mac: m1\n"
    tree.write(SECRETS, secrets)
    tree.write("ansible/deploy.yml", "- hosts: all\n  vars:\n    p: '{{ s_key }}'\n")
    tree.commit("a play reads a secret")
    tree.write(SECRETS, secrets.replace("aaa", "bbb").replace("m1", "m2"))
    rc, out = _setup_readers(tree, capsys, _moved(tree, '"3.14.6"', '"3.15.2"'))
    assert (rc, out["paths"]) == (0, [f"{ROLE}/templates/alpha.conf.j2"])
    assert "s_key" in out["refused"]


def test_role_tags_narrows_a_moved_key_to_its_readers_tag(tree):
    """No path under the role moved, and the role still needs the tag that renders the key."""
    assert narrow(tree, *_moved(tree, '"3.14.6"', '"3.15.2"')) == frozenset({"alpha"})


def test_a_host_vars_key_is_scoped_to_its_host(tree):
    """#6968: a `host_vars/<h>.yml` key reaches `<h>`, and a carried value keeps that host."""
    tree.write("ansible/inventory/host_vars/daniel-server.yml", 'server_pin: "1"\n')
    tree.commit("host vars")
    tree.write("ansible/inventory/host_vars/daniel-server.yml", 'server_pin: "2"\n')
    tree.write(
        GROUP_VARS,
        BASE_VARS.replace('"3.14.6"', '"{{ server_pin }}"'),
    )
    old, new = _refs(tree)
    ctx = broad.Context(tree.root, new, set(), {}, lambda _m: None)
    found = inventory_keys.setup_reader_paths_by_host(old, ctx)
    assert {h: sorted(r.paths) for h, r in found.items()} == {
        "": [f"{ROLE}/templates/alpha.conf.j2"],
        "daniel-server": [f"{ROLE}/templates/alpha.conf.j2"],
    }


_SECRET = "s_key: ENC[AES256_GCM,data:aaa]\nsops:\n    mac: m1\n"


def _secret_and_pin_moved(tree: Tree) -> str:
    """A range moving `s_key`, which `beta` reads, and the pin, which `alpha` reads."""
    tree.write(SECRETS, _SECRET)
    tree.write(f"{ROLE}/templates/beta.conf.j2", "token = {{ s_key }}\n")
    tree.commit("beta reads a secret")
    tree.write(SECRETS, _SECRET.replace("aaa", "bbb").replace("m1", "m2"))
    return "..".join(_moved(tree, '"3.14.6"', '"3.15.2"'))


def test_key_readers_merges_the_secrets_half_beside_the_inventory_half(tree):
    """#6968: both halves land under every host, neither replacing the other."""
    assert key_readers(_secret_and_pin_moved(tree), tree.root) == [
        KeyReader(f"{ROLE}/templates/alpha.conf.j2", frozenset()),
        KeyReader(f"{ROLE}/templates/beta.conf.j2", frozenset()),
    ]


def test_key_readers_drops_a_refusing_secrets_half_and_keeps_the_inventory_half(tree):
    """A play reading the secret refuses that half, which the tick routes nothing for."""
    tree.write("ansible/deploy.yml", "- hosts: all\n  vars:\n    p: '{{ s_key }}'\n")
    tree.commit("a play reads the secret")
    assert key_readers(_secret_and_pin_moved(tree), tree.root) == [
        KeyReader(f"{ROLE}/templates/alpha.conf.j2", frozenset())
    ]
