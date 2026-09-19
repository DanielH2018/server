"""Every atom recorded in docs/facts.lock must hash now as it did when its section was verified.

This is the repo half of the fact-support design: a CLAUDE.md section's status is derived
from the atoms it cites, and the PR that moves one of them fails here, naming the section.
The author edits the section or re-runs `scripts/dev/fact_status.py verify` on it — either
way the rule and its support change in the same PR. Spec:
docs/superpowers/specs/2026-09-19-fact-support-invalidation-design.md
"""

import subprocess

from _helpers import REPO
from lib.facts.lock import LOCK_REL, check_lock, read_lock


def test_every_recorded_atom_hashes_as_recorded():
    findings = check_lock(REPO, REPO / LOCK_REL)
    assert not findings, (
        "\n".join(
            f"{f.unit} · {f.atom or '-'} · {f.kind}: {f.detail}" for f in findings
        )
        + "\n\nEdit the section, or: uv run python scripts/dev/fact_status.py verify '<unit>'"
    )


def test_the_lock_records_at_least_the_first_converted_section():
    # Non-vacuity: a lock that silently emptied would pass the test above.
    assert "CLAUDE.md#The procedure" in read_lock(REPO / LOCK_REL)


def test_a_moved_atom_is_flagged(tmp_path):
    env = {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "HOME": str(tmp_path),
        "PATH": "/usr/bin:/bin",
    }
    subprocess.run(
        ["git", "init", "-q", "-b", "master", str(tmp_path)], check=True, env=env
    )
    # Nested a directory deep: the citation grammar requires a slash in a path
    # citation (a bare filename is not a claim about the tree — see citations.py),
    # so a repo-root `m.py` would never parse as a citation at all.
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "m.py").write_text("LIMIT = 85\n")
    (tmp_path / "CLAUDE.md").write_text("## Gate\n`sub/m.py:LIMIT`\n")
    (tmp_path / "docs").mkdir()
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, env=env)
    from lib.facts.lock import verify_units

    verify_units(tmp_path, tmp_path / LOCK_REL, ["CLAUDE.md#Gate"], "abc")
    (tmp_path / "sub" / "m.py").write_text("LIMIT = 86\n")
    assert [f.kind for f in check_lock(tmp_path, tmp_path / LOCK_REL)] == ["moved"]
