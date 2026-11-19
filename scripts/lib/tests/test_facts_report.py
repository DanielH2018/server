"""`fact_status.py report`: the backlog ranked by reads, the heads and the session bytes."""

from datetime import datetime, timedelta, timezone

from lib.facts.citations import Citation
from lib.facts.report import build, doc_reads, ranked_backlog, render, session_bytes
from lib.git_testing import git, git_out, init_repo, scrubbed_env

# A fixed epoch: the log rows and commit dates are set against it, never the live clock.
_NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)


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


def test_the_docs_read_as_a_head_are_ranked_by_reads(tmp_path):
    stamp = _NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
    old = (_NOW - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
    tail = "bash_path_match  Project "
    log = tmp_path / "instructions.log"
    (tmp_path / "instructions.log.1").write_text(
        f"{stamp} [aaaaaaaa] {tail} b/CLAUDE.md trigger=b/x form=head bytes=9\n"
    )
    log.write_text(
        f"{stamp} [aaaaaaaa] {tail} a/CLAUDE.md trigger=a/x form=head bytes=9\n"
        f"{stamp} [bbbbbbbb] {tail} .claude/rules/r.md trigger=b/x agent=z form=head bytes=9\n"
        f"{stamp} [bbbbbbbb] {tail} b/CLAUDE.md trigger=b/y form=head bytes=9\n"
        f"{stamp} [cccccccc] {tail} a/CLAUDE.md trigger=a/x bytes=9\n"
        f"{stamp} [cccccccc] {tail} c/CLAUDE.md trigger=c/x bytes=9\n"
        f"{old} [dddddddd] {tail} a/CLAUDE.md trigger=a/x form=head bytes=9\n"
    )
    repo = init_repo(tmp_path)
    _dated_commit(repo, "seed", 50, README="x\n")

    report = build(repo, {}, 30, log, now=_NOW, replays=False)

    assert report["heads"] == [
        {"doc": "b/CLAUDE.md", "reads": 2},
        {"doc": ".claude/rules/r.md", "reads": 1},
        {"doc": "a/CLAUDE.md", "reads": 1},
    ]
    rows = render(report, top=2).split("Docs read as a head")[1].splitlines()
    assert rows[2:5] == [
        "      2  b/CLAUDE.md",
        "      1  .claude/rules/r.md",
        "  ... 1 more; --top or --json lists them",
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
        # inject-nested-docs sizes its row by the block it injected.
        f"{stamp} [aaaaaaaa] bash_path_match  Project  b/CLAUDE.md trigger=b/y.py bytes=700\n"
        # A session whose rows predate the field is left out, not listed as 0.
        f"{stamp} [bbbbbbbb] session_start    Project  CLAUDE.md\n"
    )

    rows = session_bytes(log, _NOW - timedelta(days=30))

    assert [(r.session, r.loads, r.start, r.total) for r in rows] == [
        ("aaaaaaaa", 3, 3000, 6700)
    ]


def test_a_report_without_replays_leaves_out_the_comment_replay_and_still_ranks_the_backlog(
    tmp_path,
):
    repo = init_repo(tmp_path)
    _dated_commit(repo, "seed", 2, README="x\n")

    report = build(
        repo, {"CLAUDE.md#Gate": []}, 30, tmp_path / "no.log", now=_NOW, replays=False
    )

    assert "comment_replay" not in report
    assert report["backlog"] == [{"section": "CLAUDE.md#Gate", "reads": 0}]
    assert "Comment-class replay" not in render(report, top=5)


def test_a_report_with_replays_carries_the_comment_replay(tmp_path):
    repo = init_repo(tmp_path)
    _dated_commit(repo, "seed", 2, README="x\n")

    report = build(repo, {}, 30, tmp_path / "no.log", now=_NOW)

    assert report["comment_replay"]["commits"] == 1
    assert "Comment-class replay" in render(report, top=5)
