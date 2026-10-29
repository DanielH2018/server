"""What the delta review's findings count as once the fix head's file is checked (#6547).

A finding the delta reviewer repeats counts as left only if it quotes a line the file has at
the fix head; the rest are carried unverified. `FakeRunner.tree` is that head.

Run: uv run pytest scripts/dev/tests/test_fanout_review_delta.py
"""

import json

from _review_fakes import PR, _finding, _pipeline, _report


def _record(tmp_path):
    (path,) = (tmp_path / "state").glob("1345-*.json")
    return json.loads(path.read_text())


def _fix_round(left):
    """Reports for a batch whose fix round leaves `left` behind, then lands."""
    first = _finding("off by one", confidence=0.9)
    return [
        _report(f"Opened {PR}"),
        _report(structured={"summary": "", "findings": [first]}),
        _report(f"Fixed. {PR}"),
        _report(structured={"summary": "", "findings": left}),
        _report(f"Filed #9. {PR}"),
    ]


def test_a_leftover_the_fix_head_no_longer_holds_is_carried_unverified_not_left(
    tmp_path,
):
    """#6547: the delta review repeated a finding whose line the fix commit had rewritten."""
    stale = _finding("still off by one", severity="medium", confidence=0.8)
    stale["evidence"] = "the line the fix rewrote"
    pipeline, run = _pipeline(tmp_path, _fix_round([stale]))
    final = pipeline.run_all()
    assert not final["result"].startswith("needs input:")
    assert len(run.lands) == 2
    assert "0 left after the fix round" in run.comments[0]
    assert "1 more carried unverified" in run.comments[0]
    record = _record(tmp_path)
    assert record["remaining"] == []
    assert [f["title"] for f in record["unverified"]] == ["still off by one"]
    # Nothing is left, so nothing is filed.
    assert "file" not in [phase for _, _, phase in run.claude]


def test_a_leftover_without_a_quote_or_a_file_at_the_fix_head_is_unverified(tmp_path):
    no_quote = _finding("no quote", severity="medium", confidence=0.8)
    del no_quote["evidence"]
    gone = _finding("gone", severity="medium", confidence=0.8)
    gone["file"] = "scripts/deleted.py"
    pipeline, _run = _pipeline(tmp_path, _fix_round([no_quote, gone]))
    pipeline.run_all()
    record = _record(tmp_path)
    assert record["remaining"] == []
    assert len(record["unverified"]) == 2


def test_a_leftover_whose_line_moved_a_few_lines_is_still_confirmed(tmp_path):
    moved = _finding("still off by one", severity="medium", confidence=0.8)
    moved["line"] = 3 + 7
    pipeline, run = _pipeline(tmp_path, _fix_round([moved]))
    final = pipeline.run_all()
    assert final["result"].startswith("needs input:")
    assert "1 left after the fix round" in run.comments[0]
    assert "carried unverified" not in run.comments[0]
    assert _record(tmp_path)["unverified"] == []
