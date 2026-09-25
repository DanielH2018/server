"""A script whose path the caller assembles from segments is still a script it runs.

`deploy_io.staging_expect_script` builds `os.path.join(repo, "scripts", "deploy_tools",
"staging_expectations.py")`, so no string literal in that file spells the filename next to its
directory. The caller census matched a whole-path literal only, and `docs/reference/scripts.md`
called both staging scripts "no automated caller in the tree" while the GitOps deployer ran
them after every staging deploy that exits 0 (#2424).

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


def test_the_live_tree_sees_the_deployer_running_both_staging_scripts(live_verdicts):
    """Non-vacuity: the two rows #2424 was filed about, named so a silent narrowing fails.

    `deploy_io.py` sits under a role's `files/`, which the census reads as a deploy-time
    caller, so both are `gate` rather than scripts nobody runs.
    """
    for name in ("staging_expectations.py", "staging_gate.py"):
        verdict, evidence = live_verdicts[name]
        assert verdict == "gate", name
        assert "deploy_io.py" in evidence, name
