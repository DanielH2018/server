"""An inventory key a range moved reaches the setup roles that template it (#6962).

PR #6952 moved `host_python_version` in `group_vars/all.yml`. Eight setup roles render it into
their units, and the tick routed the key to the deploy plane alone. The fixture is
`_setup_role_fixtures.build`'s `demo` role, whose `alpha` template reads the key, beside an
inventory and a play.
"""

import json

import pytest

import secret_setup_readers
from lib.exit_codes import DEPLOY_BROAD

from _narrow_fixtures import Tree, _refs
from _setup_role_fixtures import ROLE, build, narrow

GROUP_VARS = "ansible/inventory/group_vars/all.yml"
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
    t.write("ansible/inventory/hosts.ini", "[all]\ndaniel-box\n")
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
        {"paths": [f"{ROLE}/templates/alpha.conf.j2"], "unread": {}},
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
        {"paths": [f"{ROLE}/templates/alpha.conf.j2"], "unread": {}},
    )


def test_a_value_built_from_the_moved_key_reaches_its_readers(tree, capsys):
    tree.write(f"{ROLE}/templates/beta.conf.j2", "uv = {{ uv_pin }}\n")
    tree.commit("beta reads the derived pin")
    refs = _moved(tree, '"3.14.6"', '"3.15.2"')
    assert _setup_readers(tree, capsys, refs)[1]["paths"] == [
        f"{ROLE}/templates/alpha.conf.j2",
        f"{ROLE}/templates/beta.conf.j2",
    ]


def test_a_shared_template_a_setup_role_names_refuses(tree, capsys):
    """The walk cannot follow the key through the macro to the role's tags."""
    tree.write("ansible/templates/python.j2", "{{ host_python_version }}\n")
    tree.write(f"{ROLE}/templates/beta.conf.j2", "{% include 'python.j2' %}\n")
    tree.commit("a macro carries the key")
    refs = _moved(tree, '"3.14.6"', '"3.15.2"')
    assert _setup_readers(tree, capsys, refs) == (DEPLOY_BROAD, {})


def test_role_tags_narrows_a_moved_key_to_its_readers_tag(tree):
    """No path under the role moved, and the role still needs the tag that renders the key."""
    assert narrow(tree, *_moved(tree, '"3.14.6"', '"3.15.2"')) == frozenset({"alpha"})
