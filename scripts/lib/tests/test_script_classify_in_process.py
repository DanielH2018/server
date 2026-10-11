"""A script another one imports and runs in process inherits that caller's kind.

`deploy_lib/run.py` calls `staleness.main` on every deploy. Without this rule the helper
reads as a `library`, which says a break costs nothing unattended when it stops every deploy. A
plain module with no `__main__` guard stays a library: it has no run of its own to inherit.

Run: uv run pytest scripts/lib/tests/test_script_classify_in_process.py
"""

from lib import script_classify as sc


def _repo(tmp_path, helper: str):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "gated.py").write_text('"""Summary."""\nimport helper\n')
    (scripts / "helper.py").write_text(helper)
    (tmp_path / "prek.toml").write_text('entry = "uv run python scripts/gated.py"\n')
    return tmp_path, scripts


def test_an_imported_script_with_a_main_guard_is_a_gate_is_clean(tmp_path):
    repo, scripts = _repo(
        tmp_path,
        '"""Summary."""\ndef main():\n    return 0\nif __name__ == "__main__":\n    main()\n',
    )
    assert sc.classify(repo, scripts)["helper.py"] == (
        "gate",
        f"gated.py ({sc.RUNS['gate']})",
    )


def test_an_imported_module_without_one_stays_a_library_is_flagged(tmp_path):
    # The guard's text in a string is not a guard; this module only mentions it.
    repo, scripts = _repo(
        tmp_path, '"""Summary."""\nGUARD = \'__name__ == "__main__"\'\n'
    )
    assert sc.classify(repo, scripts)["helper.py"][0] == "library"


def _dispatch_repo(tmp_path, target: str):
    """`gated.py` dispatches to `target.py`, which calls `helper.py` in process."""
    repo, scripts = _repo(
        tmp_path,
        '"""Summary."""\ndef main():\n    return 0\nif __name__ == "__main__":\n    main()\n',
    )
    (scripts / "gated.py").write_text('"""Summary."""\nimport target\n')
    (scripts / "target.py").write_text(target)
    return repo, scripts


def test_what_a_dispatch_target_runs_inherits_the_dispatchers_kind_is_clean(tmp_path):
    """`deploy_cli.py` calls `run.main`, and `run` calls `staleness.main` (#4347)."""
    repo, scripts = _dispatch_repo(
        tmp_path, '"""Summary."""\nimport helper\ndef main(argv=None):\n    return 0\n'
    )
    verdicts = sc.classify(repo, scripts)
    assert verdicts["helper.py"][0] == "gate"
    assert verdicts["target.py"][0] == "library"


def test_a_target_with_no_main_passes_no_kind_on_is_flagged(tmp_path):
    repo, scripts = _dispatch_repo(tmp_path, '"""Summary."""\nimport helper\n')
    assert sc.classify(repo, scripts)["helper.py"][0] != "gate"
