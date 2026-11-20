"""`codemod_imports` dots an area's bare sibling imports and deletes only the inserts it kills.

Run: uv run pytest scripts/dev/tests/test_codemod_imports.py
"""

from dev.codemod_imports import qualify_file, strip_file

INSERT = "_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))\n"
OWN_DIR = "_sys.path.insert(0, str(_Path(__file__).resolve().parent))\n"
NAMES = frozenset({"fanout_lib", "prune_worktrees"})


def _module(tmp_path, body: str):
    scripts = tmp_path / "scripts"
    path = scripts / "area" / "mod.py"
    path.parent.mkdir(parents=True)
    path.write_text(body, encoding="utf-8")
    return scripts, path


def test_an_insert_of_scripts_and_its_comment_are_removed(tmp_path):
    scripts, path = _module(
        tmp_path, f"import sys as _sys\n# Reach lib.\n{INSERT}from lib import x\n"
    )
    assert strip_file(path, frozenset({scripts})) == (1, 0)
    assert path.read_text() == "import sys as _sys\nfrom lib import x\n"


def test_an_insert_of_another_directory_is_kept(tmp_path):
    body = '_sys.path.insert(0, str(_Path(__file__).resolve().parents[2] / "hosts" / "lib"))\n'
    scripts, path = _module(tmp_path, body)
    assert strip_file(path, frozenset({scripts})) == (0, 1)
    assert path.read_text() == body


def test_an_own_directory_insert_goes_only_once_its_area_is_dead(tmp_path):
    scripts, path = _module(tmp_path, OWN_DIR)
    assert strip_file(path, frozenset({scripts})) == (0, 1)
    assert strip_file(path, frozenset({scripts, scripts / "area"})) == (1, 0)


def test_a_decided_comment_above_the_insert_is_kept(tmp_path):
    scripts, path = _module(tmp_path, f"# DECIDED: keep this.\n{INSERT}")
    assert strip_file(path, frozenset({scripts})) == (1, 0)
    assert path.read_text() == "# DECIDED: keep this.\n"


def test_each_bare_import_shape_is_dotted_under_the_area(tmp_path):
    _, path = _module(
        tmp_path,
        "import prune_worktrees as pw\n"
        "from fanout_lib import place\n"
        "def f():\n"
        "    from fanout_lib.review import api  # noqa: E402\n",
    )
    assert qualify_file(path, "dev", NAMES) == 3
    assert path.read_text() == (
        "from dev import prune_worktrees as pw\n"
        "from dev.fanout_lib import place\n"
        "def f():\n"
        "    from dev.fanout_lib.review import api  # noqa: E402\n"
    )


def test_a_parenthesised_import_keeps_its_names_and_comments(tmp_path):
    body = "from fanout_lib.place import (\n    a,  # why a\n    b,\n)\n"
    _, path = _module(tmp_path, body)
    assert qualify_file(path, "dev", NAMES) == 1
    assert path.read_text() == body.replace("from fanout_lib", "from dev.fanout_lib")


def test_an_import_outside_the_area_or_already_dotted_is_left_alone(tmp_path):
    body = "import json\nfrom lib import git\nfrom dev.fanout_lib import place\nfrom . import x\n"
    _, path = _module(tmp_path, body)
    assert qualify_file(path, "dev", NAMES) == 0
    assert path.read_text() == body
