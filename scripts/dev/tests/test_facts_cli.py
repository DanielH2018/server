"""fact_status.py: exit codes and the text a CI failure prints."""

from fact_status import _USAGE, main
from lib.git_testing import git, init_repo
from lib.facts.lock import read_lock


def _repo(tmp_path):
    init_repo(tmp_path)
    (tmp_path / "t").mkdir()
    (tmp_path / "t" / "m.py").write_text("LIMIT = 85\nOTHER = 1\n")
    (tmp_path / "CLAUDE.md").write_text(
        "## Gate\n`t/m.py:LIMIT` bounds it.\n\n## Style\nwhy, not what.\n"
    )
    (tmp_path / "docs").mkdir()
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "i", "--no-gpg-sign")
    return tmp_path


def test_status_lists_every_section_with_its_status(tmp_path, capsys):
    repo = _repo(tmp_path)
    assert (
        main(["status", "--repo", str(repo)]) == 0
    )  # Gate is UNVERIFIED: cited, no lock row at all
    out = capsys.readouterr().out
    assert "UNVERIFIED  CLAUDE.md#Gate" in out and "CONVENTION  CLAUDE.md#Style" in out


def test_new_citation_after_verify_is_out(tmp_path, capsys):
    repo = _repo(tmp_path)
    main(["verify", "--repo", str(repo), "CLAUDE.md#Gate"])
    capsys.readouterr()
    (repo / "CLAUDE.md").write_text(
        "## Gate\n`t/m.py:LIMIT` bounds it. `t/m.py:OTHER` too.\n\n## Style\nwhy, not what.\n"
    )
    assert main(["status", "--repo", str(repo)]) == 1
    out = capsys.readouterr().out
    assert "OUT  CLAUDE.md#Gate" in out


def test_verify_then_status_is_clean(tmp_path, capsys):
    repo = _repo(tmp_path)
    assert main(["verify", "--repo", str(repo), "CLAUDE.md#Gate"]) == 0
    assert (repo / "docs" / "facts.lock").is_file()
    assert main(["status", "--repo", str(repo)]) == 0
    assert "IN  CLAUDE.md#Gate" in capsys.readouterr().out


def test_moved_atom_fails_status_naming_the_section(tmp_path, capsys):
    repo = _repo(tmp_path)
    main(["verify", "--repo", str(repo), "CLAUDE.md#Gate"])
    capsys.readouterr()
    (repo / "t" / "m.py").write_text("LIMIT = 90\n")
    assert main(["status", "--repo", str(repo)]) == 1
    out = capsys.readouterr().out
    assert "CLAUDE.md#Gate" in out and "moved" in out


def test_verify_unknown_unit_is_usage_error(tmp_path, capsys):
    repo = _repo(tmp_path)
    assert main(["verify", "--repo", str(repo), "CLAUDE.md#Nope"]) == 2
    assert "no section" in capsys.readouterr().err


def test_forget_then_status_is_clean(tmp_path, capsys):
    repo = _repo(tmp_path)
    main(["verify", "--repo", str(repo), "CLAUDE.md#Gate"])
    (repo / "CLAUDE.md").write_text(
        "## The gate\n`t/m.py:LIMIT` bounds it.\n\n## Style\nwhy, not what.\n"
    )
    main(["verify", "--repo", str(repo), "CLAUDE.md#The gate"])
    capsys.readouterr()
    assert main(["status", "--repo", str(repo)]) == 1
    assert "section-gone" in capsys.readouterr().out
    assert main(["forget", "--repo", str(repo), "CLAUDE.md#Gate"]) == 0
    assert main(["status", "--repo", str(repo)]) == 0


def test_forget_unknown_unit_is_usage_error(tmp_path, capsys):
    repo = _repo(tmp_path)
    assert main(["forget", "--repo", str(repo), "CLAUDE.md#Nope"]) == _USAGE
    assert "no lock row" in capsys.readouterr().err


def test_verify_reports_skipped_atoms(tmp_path, capsys):
    repo = _repo(tmp_path)
    (repo / "CLAUDE.md").write_text(
        "## Gate\n`t/m.py:LIMIT` bounds it, `t/m.py:GONE` does not exist.\n"
    )
    assert main(["verify", "--repo", str(repo), "CLAUDE.md#Gate"]) == 0
    assert "skipped 1 unresolved: t/m.py:GONE" in capsys.readouterr().out


def test_verify_warns_on_a_dirty_tree(tmp_path, capsys):
    repo = _repo(tmp_path)
    (repo / "t" / "m.py").write_text("LIMIT = 85\nOTHER = 1\nEXTRA = 2\n")
    assert main(["verify", "--repo", str(repo), "CLAUDE.md#Gate"]) == 0
    assert "working tree is dirty" in capsys.readouterr().err


def test_lint_changed_since_scopes_to_edited_sections(tmp_path, capsys):
    repo = _repo(tmp_path)
    (repo / "CLAUDE.md").write_text(
        "## Gate\n`t/m.py:LIMIT` bounds it.\n\n## Style\nsee `t/m.py:3`\n"
    )
    assert main(["lint", "--repo", str(repo), "--changed-since", "HEAD"]) == 1
    out = capsys.readouterr().out
    assert "CLAUDE.md#Style" in out and "CLAUDE.md#Gate" not in out


def test_lint_with_unresolvable_ref_is_usage_error(tmp_path, capsys):
    repo = _repo(tmp_path)
    assert (
        main(["lint", "--repo", str(repo), "--changed-since", "origin/master"])
        == _USAGE
    )
    assert "cannot resolve 'origin/master'" in capsys.readouterr().err


def test_lint_warning_only_exits_zero(tmp_path):
    repo = _repo(tmp_path)
    (repo / "CLAUDE.md").write_text("## Gate\n`t/m.py:LIMIT` bounds 13 entries.\n")
    assert main(["lint", "--repo", str(repo)]) == 0


def test_verify_unverified_covers_every_section_with_no_row(tmp_path, capsys):
    repo = _repo(tmp_path)
    assert main(["verify", "--repo", str(repo), "--unverified"]) == 0
    capsys.readouterr()
    assert main(["status", "--repo", str(repo)]) == 0
    assert "IN  CLAUDE.md#Gate" in capsys.readouterr().out


def test_verify_unverified_leaves_a_moved_atom_out(tmp_path, capsys):
    """The flag raises coverage; it can never launder a section already in the lock."""
    repo = _repo(tmp_path)
    main(["verify", "--repo", str(repo), "CLAUDE.md#Gate"])
    (repo / "CLAUDE.md").write_text(
        "## Gate\n`t/m.py:LIMIT` bounds it.\n\n## Cap\n`t/m.py:OTHER` caps it.\n"
    )
    (repo / "t" / "m.py").write_text("LIMIT = 90\nOTHER = 1\n")
    capsys.readouterr()

    assert main(["verify", "--repo", str(repo), "--unverified"]) == 0
    assert "verified CLAUDE.md#Cap" in capsys.readouterr().out

    assert main(["status", "--repo", str(repo)]) == 1
    out = capsys.readouterr().out
    assert "OUT  CLAUDE.md#Gate" in out and "IN  CLAUDE.md#Cap" in out


def test_verify_with_no_unit_and_no_flag_is_usage_error(tmp_path, capsys):
    repo = _repo(tmp_path)
    assert main(["verify", "--repo", str(repo)]) == 2
    assert "--unverified" in capsys.readouterr().err


def test_reverify_folds_a_prose_edit_into_the_commit(tmp_path, capsys):
    repo = _repo(tmp_path)
    main(["verify", "--repo", str(repo), "CLAUDE.md#Gate"])
    (repo / "CLAUDE.md").write_text(
        "## Gate\n`t/m.py:LIMIT` bounds it. `t/m.py:OTHER` too.\n\n## Style\nwhy, not what.\n"
    )
    capsys.readouterr()

    assert main(["reverify", "--repo", str(repo), "--changed-since", "HEAD"]) == 1
    captured = capsys.readouterr()
    assert "re-verified CLAUDE.md#Gate" in captured.out
    assert "git add docs/facts.lock" in captured.err
    assert main(["status", "--repo", str(repo)]) == 0


def test_reverify_refuses_a_moved_atom(tmp_path, capsys):
    repo = _repo(tmp_path)
    main(["verify", "--repo", str(repo), "CLAUDE.md#Gate"])
    (repo / "CLAUDE.md").write_text(
        "## Gate\n`t/m.py:LIMIT` bounds it. `t/m.py:OTHER` too.\n\n## Style\nwhy, not what.\n"
    )
    (repo / "t" / "m.py").write_text("LIMIT = 90\nOTHER = 1\n")
    capsys.readouterr()

    assert main(["reverify", "--repo", str(repo), "--changed-since", "HEAD"]) == 1
    assert "moved" in capsys.readouterr().err
    assert main(["status", "--repo", str(repo)]) == 1


def test_reverify_is_quiet_when_nothing_changed(tmp_path, capsys):
    repo = _repo(tmp_path)
    main(["verify", "--repo", str(repo), "CLAUDE.md#Gate"])
    capsys.readouterr()
    assert main(["reverify", "--repo", str(repo), "--changed-since", "HEAD"]) == 0
    assert capsys.readouterr().out == ""


def test_reverify_with_unresolvable_ref_is_usage_error(tmp_path, capsys):
    repo = _repo(tmp_path)
    assert main(["reverify", "--repo", str(repo), "--changed-since", "nope"]) == 2
    assert "cannot resolve" in capsys.readouterr().err


def test_verify_unverified_records_a_probe_only_section_as_unknown(tmp_path, capsys):
    """The row holds no hash, and it is still what takes the section out of UNVERIFIED."""
    repo = _repo(tmp_path)
    (repo / "CLAUDE.md").write_text(
        "## Gate\n`t/m.py:LIMIT` bounds it.\n\n## Down\n`probe.py monitors` answers it.\n"
    )
    assert main(["verify", "--repo", str(repo), "--unverified"]) == 0
    assert read_lock(repo / "docs" / "facts.lock")["CLAUDE.md#Down"]["atoms"] == {}
    capsys.readouterr()
    assert main(["status", "--repo", str(repo)]) == 0
    assert "UNKNOWN  CLAUDE.md#Down" in capsys.readouterr().out


def test_reverify_names_a_moved_atom_in_a_section_the_commit_did_not_edit(
    tmp_path, capsys
):
    """CI's next failure is cheaper to read here than from the run."""
    repo = _repo(tmp_path)
    main(["verify", "--repo", str(repo), "CLAUDE.md#Gate"])
    (repo / "CLAUDE.md").write_text(
        "## Gate\n`t/m.py:LIMIT` bounds it.\n\n## Style\nwhy, not what. Reworded.\n"
    )
    (repo / "t" / "m.py").write_text("LIMIT = 90\nOTHER = 1\n")
    capsys.readouterr()

    assert main(["reverify", "--repo", str(repo), "--changed-since", "HEAD"]) == 1
    err = capsys.readouterr().err
    assert "not folded, and CI fails on these:" in err
    assert "moved: CLAUDE.md#Gate" in err
