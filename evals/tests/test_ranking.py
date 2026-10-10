import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ranking import rank_docs, section_reads


def test_rank_docs_counts_repo_docs_and_drops_user_level_paths():
    log = [
        "t [s] session_start Project CLAUDE.md",
        "t [s] session_start User /home/u/.claude/CLAUDE.md",
        "t [s] session_start User /home/u/.claude/CLAUDE.md",
        "t [s] bash_path_match Project roles/x/CLAUDE.md trigger=roles/x/a.py",
        "t [s] path_glob_match Project roles/x/CLAUDE.md trigger=roles/x/b.py",
        "malformed",
    ]
    assert rank_docs(log) == [("roles/x/CLAUDE.md", 2), ("CLAUDE.md", 1)]


def _turn(role, content):
    return json.dumps({"type": role, "message": {"role": role, "content": content}})


def test_section_reads_count_what_the_assistant_wrote_once_per_transcript(
    tmp_path: Path,
):
    headings = ["Alpha — the long tail", "Beta (aside)", "Gamma"]
    (tmp_path / "one.jsonl").write_text(
        "\n".join(
            [
                # A user turn and a tool result carry the whole doc whenever it loads.
                _turn("user", "## Alpha — the long tail\n## Beta (aside)\n## Gamma"),
                _turn("user", [{"type": "tool_result", "content": "Gamma"}]),
                _turn("assistant", [{"type": "text", "text": "per Alpha, and Alpha"}]),
                _turn("assistant", [{"type": "text", "text": "Alpha again"}]),
            ]
        )
    )
    (tmp_path / "two.jsonl").write_text(
        _turn(
            "assistant",
            [
                {"type": "thinking", "thinking": "Beta says so"},
                {"type": "tool_use", "input": {"pattern": "Alpha"}},
            ],
        )
    )
    paths = sorted(tmp_path.glob("*.jsonl"))
    assert section_reads(headings, paths) == [
        ("Alpha — the long tail", 2),
        ("Beta (aside)", 1),
        ("Gamma", 0),
    ]


def test_a_sessions_subagents_count_as_that_one_session(tmp_path: Path):
    cite = _turn("assistant", [{"type": "text", "text": "per Alpha"}])
    (tmp_path / "s1.jsonl").write_text(cite)
    (tmp_path / "s1" / "subagents").mkdir(parents=True)
    for agent in ("a", "b"):
        (tmp_path / "s1" / "subagents" / f"{agent}.jsonl").write_text(cite)
    (tmp_path / "s2" / "subagents").mkdir(parents=True)
    (tmp_path / "s2" / "subagents" / "a.jsonl").write_text(cite)
    paths = sorted(tmp_path.rglob("*.jsonl"))
    assert section_reads(["Alpha"], paths) == [("Alpha", 2)]
