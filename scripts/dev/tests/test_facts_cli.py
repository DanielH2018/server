"""fact_status.py: exit codes and the text a CI failure prints."""

import subprocess

from fact_status import _USAGE, main


def _repo(tmp_path):
    env = {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "HOME": str(tmp_path),
        "PATH": "/usr/bin:/bin",
    }
    subprocess.run(
        ["git", "init", "-q", "-b", "master", str(tmp_path)], check=True, env=env
    )
    (tmp_path / "t").mkdir()
    (tmp_path / "t" / "m.py").write_text("LIMIT = 85\nOTHER = 1\n")
    (tmp_path / "CLAUDE.md").write_text(
        "## Gate\n`t/m.py:LIMIT` bounds it.\n\n## Style\nwhy, not what.\n"
    )
    (tmp_path / "docs").mkdir()
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, env=env)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@x",
            "commit",
            "-q",
            "-m",
            "i",
            "--no-gpg-sign",
        ],
        cwd=tmp_path,
        check=True,
        env=env,
    )
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
