"""A script whose path the caller assembles from segments is still a script it runs.

A caller can build `os.path.join(repo, "scripts", "deploy_tools", "staging_expectations.py")`,
so no string literal in that file spells the filename next to its directory. A caller census
that matched a whole-path literal only would let `docs/reference/scripts.md` call the script
"no automated caller in the tree".

The tree holds no assembled-path caller to name, so the cases below are synthetic. A real one
reaching the tree belongs here as a fourth, named case.

Run: uv run pytest scripts/lib/tests/test_script_classify_assembled_paths.py
"""

from lib import script_classify as sc


def _repo(tmp_path, body: str):
    """A tree whose only invocation site is one module under a role's `files/`."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "lonely.py").write_text('"""Summary."""\n')
    runner = tmp_path / "ansible" / "roles" / "setup" / "r" / "files" / "runner.py"
    runner.parent.mkdir(parents=True)
    runner.write_text(body)
    return tmp_path, scripts


def test_a_path_built_from_join_segments_is_an_invocation(tmp_path):
    repo, scripts = _repo(
        tmp_path,
        '"""Summary."""\nimport os\n'
        'def script(repo):\n    return os.path.join(repo, "scripts", "lonely.py")\n',
    )
    verdict, evidence = sc.classify(repo, scripts)["lonely.py"]
    assert verdict == "gate"
    assert "runner.py" in evidence


def test_a_path_built_by_dividing_a_path_object_is_an_invocation(tmp_path):
    repo, scripts = _repo(
        tmp_path,
        '"""Summary."""\nfrom pathlib import Path\n'
        'def script(repo):\n    return Path(repo) / "scripts" / "lonely.py"\n',
    )
    assert sc.classify(repo, scripts)["lonely.py"][0] == "gate"


def test_a_join_whose_filename_a_variable_supplies_is_not_an_invocation(tmp_path):
    """Only the literal segments count, so a filename the source never spells cannot slip in."""
    repo, scripts = _repo(
        tmp_path,
        '"""Summary."""\nimport os\n'
        'def script(repo, name):\n    return os.path.join(repo, "scripts", name)\n',
    )
    assert sc.classify(repo, scripts)["lonely.py"][0] == "adhoc"
