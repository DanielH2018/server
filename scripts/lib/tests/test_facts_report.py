"""`fact_status.py report`: lock precision from git history, and the backlog ranked by reads."""

from datetime import datetime, timedelta, timezone

from lib.facts.lock import LOCK_REL, verify_units
from lib.facts.report import doc_reads, precision, ranked_backlog
from lib.git_testing import commit, git, init_repo

# A fixed epoch: the log rows are dated against it, never against the live clock.
_NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
_DOC = "## Gate\n`t/m.py:LIMIT` bounds {what}.\n"


def _verified_commit(repo, message, **files):
    """One commit holding ``files`` and the lock re-verified against them, as `verify` leaves it."""
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    git(repo, "add", "-A")
    verify_units(repo, repo / LOCK_REL, ["CLAUDE.md#Gate"], message)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message, "--no-gpg-sign")


def test_a_move_with_a_prose_edit_is_actual_and_one_without_is_potential(tmp_path):
    repo = init_repo(tmp_path)
    commit(repo, "seed", README="x\n")
    _verified_commit(
        repo, "base", **{"t/m.py": "LIMIT = 1\n", "CLAUDE.md": _DOC.format(what="it")}
    )
    # The atom moves and the section is rewritten with it: the case the lock forces.
    _verified_commit(
        repo,
        "edit",
        **{"t/m.py": "LIMIT = 2\n", "CLAUDE.md": _DOC.format(what="it, now 2")},
    )
    # A refactor that moves the atom and leaves the prose alone.
    _verified_commit(repo, "refactor", **{"t/m.py": "LIMIT = 1 + 1\n"})

    counts = precision(repo, 30).forms
    assert (counts["symbol"].potential, counts["symbol"].actual) == (2, 1)
    assert all(c.potential == 0 for f, c in counts.items() if f != "symbol")


def test_the_backlog_is_ranked_by_its_docs_reads(tmp_path):
    now = _NOW
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    old = (now - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
    log = tmp_path / "instructions.log"
    (tmp_path / "instructions.log.1").write_text(
        f"{stamp} [aaaaaaaa] session_start    Project  CLAUDE.md\n"
    )
    log.write_text(
        f"{stamp} [aaaaaaaa] session_start    Project  CLAUDE.md\n"
        f"{stamp} [bbbbbbbb] path_glob_match  Project  "
        "/srv/repo/.claude/worktrees/claude+x/a/CLAUDE.md trigger=a/x.py\n"
        f"{stamp} [        ] bash_path_match  Project  /srv/repo/a/CLAUDE.md\n"
        f"{stamp} [bbbbbbbb] path_glob_match  Project  a/CLAUDE.md\n"
        f"{old} [cccccccc] path_glob_match  Project  b/CLAUDE.md\n"
        f"{stamp} [cccccccc] session_start    User     /home/u/.claude/CLAUDE.md\n"
    )
    statuses = {
        "CLAUDE.md#Root": "CONVENTION",
        "a/CLAUDE.md#Rule": "UNVERIFIED",
        "a/CLAUDE.md#Gate": "IN",
        "b/CLAUDE.md#Old": "UNKNOWN",
    }
    reads = doc_reads(
        log,
        {"CLAUDE.md", "a/CLAUDE.md", "b/CLAUDE.md"},
        now - timedelta(days=30),
        ("/srv/repo",),
    )

    assert ranked_backlog(statuses, reads) == [
        ("a/CLAUDE.md#Rule", "UNVERIFIED", 3),
        ("CLAUDE.md#Root", "CONVENTION", 2),
        ("b/CLAUDE.md#Old", "UNKNOWN", 0),
    ]


def test_a_missing_log_reads_as_no_rows(tmp_path):
    reads = doc_reads(tmp_path / "instructions.log", {"CLAUDE.md"}, _NOW)
    assert (reads.rows, dict(reads.counts)) == (0, {})
