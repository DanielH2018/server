"""`fact_status.py report`: the notice replay over the window, and the backlog ranked by reads."""

from datetime import datetime, timedelta, timezone

import pytest

from lib.facts.citations import Citation
from lib.facts.notice import Notice
from lib.facts.replay import notice_replay
from lib.facts.report import build, doc_reads, ranked_backlog, session_bytes
from lib.git_testing import commit, git, git_out, init_repo, scrubbed_env

# A fixed epoch: the log rows and commit dates are set against it, never the live clock.
_NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
_DOC = "## Gate\n`t/m.py:LIMIT` bounds {what}.\n"


def _dated_commit(repo, message, days_ago, **files):
    """One commit holding ``files``, dated ``days_ago`` days before ``_NOW``; returns its SHA."""
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    git(repo, "add", "-A")
    stamp = (_NOW - timedelta(days=days_ago)).isoformat()
    env = scrubbed_env(GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
    git(repo, "commit", "-q", "-m", message, "--no-gpg-sign", env=env)
    return git_out(repo, "rev-parse", "HEAD")


def test_the_backlog_is_the_sections_citing_nothing_ranked_by_their_docs_reads(
    tmp_path,
):
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
    by_unit = {
        "CLAUDE.md#Root": [],
        "a/CLAUDE.md#Rule": [],
        "a/CLAUDE.md#Gate": [Citation("path", "a/x.py", "a/x.py", "")],
        "b/CLAUDE.md#Old": [],
    }
    reads = doc_reads(
        log,
        {"CLAUDE.md", "a/CLAUDE.md", "b/CLAUDE.md"},
        now - timedelta(days=30),
        ("/srv/repo",),
    )

    assert ranked_backlog(by_unit, reads) == [
        ("a/CLAUDE.md#Rule", 3),
        ("CLAUDE.md#Root", 2),
        ("b/CLAUDE.md#Old", 0),
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
    since = _NOW - timedelta(days=30)
    return notice_replay(repo, since, notice) if notice else notice_replay(repo, since)


def _gate_history(repo):
    """A seed, a base citing ``LIMIT``, and a commit that moves it; returns the move's SHA."""
    commit(repo, "seed", README="x\n")
    commit(
        repo, "base", **{"t/m.py": "LIMIT = 1\n", "CLAUDE.md": _DOC.format(what="it")}
    )
    return commit(repo, "move", **{"t/m.py": "LIMIT = 2\n"})


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


def test_a_section_deleted_after_its_move_is_still_listed_at_the_move(tmp_path):
    repo = init_repo(tmp_path)
    move = _gate_history(repo)
    commit(repo, "drop the section", **{"CLAUDE.md": "## Other\nno citations\n"})

    replay = _replay(repo)

    assert {"commit": move, "unit": "CLAUDE.md#Gate"} in replay["content_notices"]


def test_a_notice_that_raises_on_one_commit_is_recorded_and_the_replay_goes_on(
    tmp_path,
):
    repo = init_repo(tmp_path)
    move = _gate_history(repo)
    seed = git(repo, "rev-list", "--max-parents=0", "HEAD").stdout.strip()

    def stub(repo, base, head):
        if base == seed:
            raise KeyError("boom")
        return [Notice("CLAUDE.md#Gate", "t/m.py:LIMIT", "symbol", "moved", False)]

    replay = _replay(repo, stub)

    [crash] = replay["crashed"]
    assert crash["error"] == "KeyError"
    assert replay["content_notices"] == [{"commit": move, "unit": "CLAUDE.md#Gate"}]
    assert replay["commits"] == 3


@pytest.mark.parametrize("with_lock_commit", [True, False], ids=["lock", "no-lock"])
def test_the_replay_window_is_the_plain_days_boundary(tmp_path, with_lock_commit):
    repo = init_repo(tmp_path)
    _dated_commit(repo, "seed", 50, README="x\n")
    # A commit writing the lock 40 days back: the last lock commit before the 30-day boundary.
    lock = {"docs/facts.lock": '{"units": {}}\n'} if with_lock_commit else {}
    _dated_commit(repo, "lock", 40, **{"t/m.py": "LIMIT = 1\n", **lock})
    _dated_commit(repo, "outside", 35, **{"t/m.py": "LIMIT = 2\n"})
    inside = [
        _dated_commit(repo, "inside 1", 5, **{"t/m.py": "LIMIT = 3\n"}),
        _dated_commit(repo, "inside 2", 1, **{"t/m.py": "LIMIT = 4\n"}),
    ]
    heads = []

    def stub(repo, base, head):
        heads.append(head)
        return []

    report = build(repo, {}, 30, tmp_path / "no.log", now=_NOW, notice=stub)

    assert heads == inside
    assert report["notice_replay"]["commits"] == 2
