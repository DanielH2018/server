"""An inventory key a range moved reaches the setup roles that template it (#6962).

PR #6952 moved `host_python_version` in `group_vars/all.yml`. Eight setup roles render it into
their units, and the tick routed the key to the deploy plane alone. The fixture is
`_setup_role_fixtures.build`'s `demo` role, whose `alpha` template reads the key, beside an
inventory and a play.
"""

import json

import pytest

import narrow_setup
import secret_setup_readers

from _narrow_fixtures import Tree, _refs
from _setup_role_fixtures import BETA, ROLE, build, narrow

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


def _beta_ships(tree: Tree, monkeypatch, shipped: str, text: str) -> None:
    """`beta`'s task ships `shipped`, another role's file, which names the moved key."""
    monkeypatch.setitem(
        narrow_setup.deploy_cross_role.SETUP_FILES_SHIPPED_BY_OTHER_ROLES,
        shipped,
        frozenset({"demo"}),
    )
    tree.write(shipped, text)
    task = f"- name: Ship it\n  ansible.builtin.copy:\n    src: {shipped}\n  tags: [beta]\n"
    tree.write(f"{ROLE}/tasks/beta.yml", BETA + task)
    tree.commit("beta ships another role's file")


def test_a_key_a_shipped_verbatim_file_names_adds_no_tag(tree, monkeypatch):
    """`hosts/lib/host_lib.py`'s docstring names `host_python_version`, and is copied as is."""
    _beta_ships(
        tree,
        monkeypatch,
        "hosts/lib/host_lib.py",
        '"""Runs under host_python_version."""\n',
    )
    assert narrow(tree, *_moved(tree, '"3.14.6"', '"3.15.2"')) == frozenset({"alpha"})


def _secret_moves_beside_the_key(tree: Tree) -> tuple[str, str]:
    """The range moves `s_key` in `secrets.yml` and `host_python_version` in the inventory."""
    secrets = "s_key: ENC[AES256_GCM,data:aaa]\nsops:\n    mac: m1\n"
    tree.write(SECRETS, secrets)
    tree.commit("a secret")
    tree.write(SECRETS, secrets.replace("aaa", "bbb").replace("m1", "m2"))
    return _moved(tree, '"3.14.6"', '"3.15.2"')


def test_a_secret_a_shipped_verbatim_file_names_keeps_the_narrow_answer(
    tree, monkeypatch
):
    """`hosts/lib/host_lib.py` is copied as is, so its docstring reads no secret (#6992)."""
    _beta_ships(tree, monkeypatch, "hosts/lib/host_lib.py", '"""Reads s_key."""\n')
    assert narrow(tree, *_secret_moves_beside_the_key(tree)) == frozenset({"alpha"})


def test_a_secret_a_shipped_template_names_widens_to_the_whole_role(tree, monkeypatch):
    _beta_ships(
        tree,
        monkeypatch,
        "ansible/roles/setup/common/templates/s.env.j2",
        "S={{ s_key }}\n",
    )
    with pytest.raises(narrow_setup.CannotNarrow, match="s_key"):
        narrow(tree, *_secret_moves_beside_the_key(tree))


def test_a_key_a_shipped_template_names_adds_the_shipping_tag(tree, monkeypatch):
    _beta_ships(
        tree,
        monkeypatch,
        "ansible/roles/setup/common/templates/py.conf.j2",
        "py = {{ host_python_version }}\n",
    )
    got = narrow(tree, *_moved(tree, '"3.14.6"', '"3.15.2"'))
    assert got == frozenset({"alpha", "beta"})
