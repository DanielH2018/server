"""`fact_status.py report`: lock precision from git history, and the backlog ranked by reads."""

import re
from datetime import datetime, timedelta, timezone

import pytest

from lib.facts.lock import LOCK_REL, verify_units
from lib.facts.notice import Notice
from lib.facts.replay import notice_replay, render_replay
from lib.facts.report import doc_reads, precision, ranked_backlog, session_bytes
from lib.git_testing import commit, git, init_repo

# A fixed epoch: the log rows are dated against it, never against the live clock.
_NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
_DOC = "## Gate\n`t/m.py:LIMIT` bounds {what}.\n"


def _verified_commit(repo, message, units=("CLAUDE.md#Gate",), **files):
    """One commit holding ``files`` and the lock re-verified against them, as `verify` leaves it."""
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    git(repo, "add", "-A")
    verify_units(repo, repo / LOCK_REL, list(units), message)
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
        "b/CLAUDE.md#Old": "OUT",
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
        ("b/CLAUDE.md#Old", "OUT", 0),
    ]


def test_a_missing_log_reads_as_no_rows(tmp_path):
    reads = doc_reads(tmp_path / "instructions.log", {"CLAUDE.md"}, _NOW)
    assert (reads.rows, dict(reads.counts)) == (0, {})


def test_session_bytes_split_start_from_total_and_skip_unsized_and_subagent_rows(
    tmp_path,
):
    stamp = _NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
    log = tmp_path / "instructions.log"
    log.write_text(
        f"{stamp} [aaaaaaaa] session_start    Project  CLAUDE.md bytes=3000\n"
        f"{stamp} [aaaaaaaa] compact          Project  CLAUDE.md bytes=3000\n"
        # A subagent's load shares the parent's id but fills its own context window.
        f"{stamp} [aaaaaaaa] session_start    Project  CLAUDE.md agent=ab12 bytes=3000\n"
        f"{stamp} [aaaaaaaa] bash_path_match  Project  a/CLAUDE.md trigger=a/x.py\n"
        # inject-nested-docs sizes its row by the block it injected (#4653).
        f"{stamp} [aaaaaaaa] bash_path_match  Project  b/CLAUDE.md trigger=b/y.py bytes=700\n"
        # A session whose rows predate the field is left out, not listed as 0.
        f"{stamp} [bbbbbbbb] session_start    Project  CLAUDE.md\n"
    )

    rows = session_bytes(log, _NOW - timedelta(days=30))

    assert [(r.session, r.loads, r.start, r.total) for r in rows] == [
        ("aaaaaaaa", 3, 3000, 6700)
    ]


def _replay(repo, notice=None):
    prec = precision(repo, 30)
    return notice_replay(repo, prec, notice) if notice else notice_replay(repo, prec)


def _gate_history(repo, **move_extra):
    """A seed, a verified base, and one lock commit that moves ``LIMIT`` with ``move_extra``."""
    commit(repo, "seed", README="x\n")
    _verified_commit(
        repo, "base", **{"t/m.py": "LIMIT = 1\n", "CLAUDE.md": _DOC.format(what="it")}
    )
    _verified_commit(repo, "move", **{"t/m.py": "LIMIT = 2\n", **move_extra})


def _nothing(repo, base, head):
    return []


def test_a_lock_move_the_notice_lists_reads_listed(tmp_path):
    repo = init_repo(tmp_path)
    _gate_history(repo)

    replay = _replay(repo)

    [move] = replay["content_lock_moves"]
    assert (move["unit"], move["atom"], move["class"]) == (
        "CLAUDE.md#Gate",
        "t/m.py:LIMIT",
        "listed",
    )
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", move["committed_at"])
    assert replay["content_notices"] == [
        {"commit": move["commit"], "unit": "CLAUDE.md#Gate"}
    ]


def test_a_lock_move_the_notice_does_not_list_reads_a_named_miss(tmp_path):
    repo = init_repo(tmp_path)
    _gate_history(repo)

    replay = _replay(repo, _nothing)

    assert [m["class"] for m in replay["content_lock_moves"]] == ["miss"]
    named = "\n".join(render_replay(replay))
    assert re.search(r"miss +\w{12} .* CLAUDE\.md#Gate  t/m\.py:LIMIT", named)


@pytest.mark.parametrize(
    "hashing_file", ["scripts/lib/facts/atoms.py", "scripts/lib/facts/citations.py"]
)
def test_a_lock_move_in_a_commit_that_touches_the_hashing_code_reads_hashing(
    tmp_path, hashing_file
):
    repo = init_repo(tmp_path)
    _gate_history(repo, **{hashing_file: "# changed\n"})

    assert [m["class"] for m in _replay(repo, _nothing)["content_lock_moves"]] == [
        "hashing"
    ]


_UV_LOCK = '[[package]]\nname = "pyyaml"\nversion = "{}"\n\n[[package]]\nname = "requests"\nversion = "{}"\n'


@pytest.mark.parametrize(
    ("versions", "expected"),
    [(("6.0.4", "2.32.0"), "hashing"), (("6.0.3", "2.33.0"), "miss")],
    ids=["pyyaml-bump", "other-package-bump"],
)
def test_only_a_pyyaml_bump_in_uv_lock_reads_hashing(tmp_path, versions, expected):
    repo = init_repo(tmp_path)
    commit(repo, "lock", **{"uv.lock": _UV_LOCK.format("6.0.3", "2.32.0")})
    _gate_history(repo, **{"uv.lock": _UV_LOCK.format(*versions)})

    assert [m["class"] for m in _replay(repo, _nothing)["content_lock_moves"]] == [
        expected
    ]


def test_moved_counts_moved_rows_and_those_whose_prose_was_edited(tmp_path):
    repo = init_repo(tmp_path)
    _gate_history(repo)
    rows = [
        Notice("CLAUDE.md#A", "t/m.py:A", "symbol", "moved", True),
        Notice("CLAUDE.md#B", "t/m.py:B", "symbol", "moved", False),
        Notice("CLAUDE.md#C", "t/c.py", "path", "gone", True),
    ]

    def stub(repo, base, head):
        return (
            rows
            if git(repo, "log", "-1", "--format=%s", head).stdout == "move\n"
            else []
        )

    replay = _replay(repo, stub)

    assert (replay["moved"], replay["moved_prose_edited"]) == (2, 1)
    assert replay["notices"] == {"gone": 1, "moved": 2}
    assert replay["commits_with_notice"] == 1


@pytest.mark.parametrize("a_also_verifies_another_row", [False, True])
def test_a_move_whose_lock_update_lands_a_commit_later_reads_split(
    tmp_path, a_also_verifies_another_row
):
    repo = init_repo(tmp_path)
    doc = _DOC.format(what="it") + "\n## Other\n`t/o.py:OTHER` too.\n"
    both = ("CLAUDE.md#Gate", "CLAUDE.md#Other")
    commit(repo, "seed", README="x\n")
    _verified_commit(
        repo,
        "base",
        units=both,
        **{"t/m.py": "LIMIT = 1\n", "t/o.py": "OTHER = 1\n", "CLAUDE.md": doc},
    )
    # Commit A moves LIMIT and leaves its hash stale; commit B records the new hash.
    if a_also_verifies_another_row:
        _verified_commit(
            repo,
            "A",
            units=("CLAUDE.md#Other",),
            **{"t/m.py": "LIMIT = 2\n", "t/o.py": "OTHER = 2\n"},
        )
    else:
        commit(repo, "A", **{"t/m.py": "LIMIT = 2\n"})
    _verified_commit(repo, "B")

    gate = [
        m["class"]
        for m in _replay(repo)["content_lock_moves"]
        if m["unit"] == "CLAUDE.md#Gate"
    ]
    assert gate == ["split"]


def test_a_section_deleted_after_its_move_is_still_listed_at_the_move(tmp_path):
    repo = init_repo(tmp_path)
    _gate_history(repo)
    commit(repo, "drop the section", **{"CLAUDE.md": "## Other\nno citations\n"})

    replay = _replay(repo)

    assert [m["class"] for m in replay["content_lock_moves"]] == ["listed"]


def test_a_notice_that_raises_on_one_commit_is_recorded_and_the_replay_goes_on(
    tmp_path,
):
    repo = init_repo(tmp_path)
    _gate_history(repo)
    seed = git(repo, "rev-list", "--max-parents=0", "HEAD").stdout.strip()

    def stub(repo, base, head):
        if base == seed:
            raise KeyError("boom")
        return [Notice("CLAUDE.md#Gate", "t/m.py:LIMIT", "symbol", "moved", False)]

    replay = _replay(repo, stub)

    [crash] = replay["crashed"]
    assert crash["error"] == "KeyError"
    assert [m["class"] for m in replay["content_lock_moves"]] == ["listed"]
    assert replay["commits"] == 3
