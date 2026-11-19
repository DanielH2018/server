"""`codemod_imports.strip_file` deletes only the inserts that name `scripts/`.

Run: uv run pytest scripts/dev/tests/test_codemod_imports.py
"""

from dev.codemod_imports import strip_file

INSERT = "_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))\n"


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
    assert strip_file(path, scripts) == (1, 0)
    assert path.read_text() == "import sys as _sys\nfrom lib import x\n"


def test_an_insert_of_another_directory_is_kept(tmp_path):
    body = '_sys.path.insert(0, str(_Path(__file__).resolve().parents[2] / "hosts" / "lib"))\n'
    scripts, path = _module(tmp_path, body)
    assert strip_file(path, scripts) == (0, 1)
    assert path.read_text() == body


def test_a_decided_comment_above_the_insert_is_kept(tmp_path):
    scripts, path = _module(tmp_path, f"# DECIDED: keep this.\n{INSERT}")
    assert strip_file(path, scripts) == (1, 0)
    assert path.read_text() == "# DECIDED: keep this.\n"
