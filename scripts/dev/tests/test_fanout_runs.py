"""`fanout.py place runs`: every run manifest on the host, so a lost run-id is recoverable.

Run: uv run pytest scripts/dev/tests/test_fanout_runs.py
"""

import json

from fanout_lib.manifest import Batch, Manifest, save
from fanout_lib.place import main
from _fanout_fakes import fake_tools


def _runs(tmp_path, *args):
    return main(["runs", *args, "--manifest-root", str(tmp_path)], fake_tools()[0])


def test_runs_lists_every_manifest_and_skips_an_unreadable_one(tmp_path, capsys):
    b1 = Batch("1", "daniel-box", "/w1", "worktree-fanout-1", "u1", [1], "t")
    b2 = Batch("2-3", "daniel-server", "/w2", "worktree-fanout-2-3", "u2", [2, 3], "t")
    save(Manifest("20260101T000010Z", "worktree-a", [b1]), root=tmp_path)
    save(Manifest("20260102T000010Z", "worktree-b", [b2]), root=tmp_path)
    (tmp_path / "20260103T000010Z.json").write_text("{truncated")
    assert _runs(tmp_path) == 0
    out = capsys.readouterr().out
    assert "20260101T000010Z  orchestrator worktree-a" in out
    assert "2-3 on daniel-server: worktree-fanout-2-3 #2,#3 standing" in out
    assert "20260103T000010Z" not in out


def test_runs_filters_by_orchestrator_and_prints_json(tmp_path, capsys):
    b1 = Batch("1", "daniel-box", "/w1", "worktree-fanout-1", "u1", [1], "t")
    save(Manifest("20260101T000010Z", "worktree-a", [b1]), root=tmp_path)
    save(Manifest("20260102T000010Z", "worktree-b", []), root=tmp_path)
    assert _runs(tmp_path, "--json", "--orchestrator", "worktree-a") == 0
    runs = json.loads(capsys.readouterr().out)
    assert [(r["run_id"], r["batches"][0]["branch"]) for r in runs] == [
        ("20260101T000010Z", "worktree-fanout-1")
    ]
    assert _runs(tmp_path, "--orchestrator", "worktree-c") == 0
    assert "no fan-out runs for worktree-c" in capsys.readouterr().out
