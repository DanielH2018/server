"""`fanout.py place runs`: every run manifest on the host, so a lost run-id is recoverable.

Run: uv run pytest scripts/dev/tests/test_fanout_runs.py
"""

import dataclasses
import json

import pytest

from dev.fanout_lib.manifest import Batch, Manifest, all_runs, load, path, save
from dev.fanout_lib.place import main
from _fanout_fakes import fake_tools, ok


def _runs(tmp_path, *args):
    return main(["runs", *args, "--manifest-root", str(tmp_path)], fake_tools()[0])


def _entry(**more) -> dict:
    return {
        "batch": "1",
        "host": "daniel-box",
        "worktree": "/w1",
        "branch": "worktree-fanout-1",
        "unit": "u1",
        "issues": [1],
        "launched_at": "t",
        **more,
    }


def _write(tmp_path, run_id: str, *entries) -> None:
    path(run_id, tmp_path).write_text(
        json.dumps(
            {"run_id": run_id, "orchestrator_branch": "worktree-a", "batches": entries}
        )
    )


def test_runs_lists_every_manifest_and_skips_an_unreadable_one(tmp_path, capsys):
    b1 = Batch("1", "daniel-box", "/w1", "worktree-fanout-1", "u1", [1], "t")
    b2 = Batch("2-3", "daniel-server", "/w2", "worktree-fanout-2-3", "u2", [2, 3], "t")
    save(Manifest("20260101T000010Z", "worktree-a", [b1]), root=tmp_path)
    save(Manifest("20260102T000010Z", "worktree-b", [b2]), root=tmp_path)
    (tmp_path / "20260103T000010Z.json").write_text("{truncated")
    _write(tmp_path, "20260104T000010Z", "not an object")
    assert _runs(tmp_path) == 0
    out = capsys.readouterr().out
    assert "20260101T000010Z  orchestrator worktree-a" in out
    assert "2-3 on daniel-server: worktree-fanout-2-3 #2,#3 standing" in out
    assert "20260103T000010Z" not in out
    assert "20260104T000010Z" not in out


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


def test_a_batch_key_this_build_does_not_know_survives_a_rewrite(tmp_path):
    run_id = "20260101T000010Z"
    _write(tmp_path, run_id, _entry(held_since="t0"))
    assert [r.run_id for r in all_runs(tmp_path)] == [run_id]
    run = load(run_id, tmp_path)
    run.batches[0] = dataclasses.replace(run.batches[0], removed_at="later")
    save(run, root=tmp_path)
    assert json.loads(path(run_id, tmp_path).read_text())["batches"] == [
        _entry(
            held_since="t0",
            removed_at="later",
            repo="DanielH2018/server",
            rung="opus-medium",
            cited_paths=None,
            excused_paths=[],
        )
    ]


@pytest.mark.parametrize("name", ["batch", "host", "worktree", "branch", "unit"])
@pytest.mark.parametrize("value", ["/w 1; touch /w2", "-oProxyCommand=x", "$(id)", ""])
def test_a_batch_field_a_command_names_must_be_a_plain_word(name, value):
    with pytest.raises(ValueError, match=f"batch {name} .* is not a plain word"):
        Batch.from_json(_entry(**{name: value}))


def test_a_run_holding_a_field_that_is_not_a_plain_word_is_not_listed(tmp_path):
    _write(tmp_path, "20260101T000010Z", _entry())
    _write(tmp_path, "20260102T000010Z", _entry(unit="u1; touch /w2"))
    assert [r.run_id for r in all_runs(tmp_path)] == ["20260101T000010Z"]


# The fields of `Batch` in the builds that read an entry with `Batch(**entry)`.
CONSTRUCTOR_READER_FIELDS = frozenset(
    {
        "batch",
        "host",
        "worktree",
        "branch",
        "unit",
        "issues",
        "launched_at",
        "removed_at",
        "repo",
        "rung",
    }
)


def test_a_saved_batch_adds_only_the_path_keys_to_what_a_constructor_reader_accepts(
    tmp_path,
):
    """The two keys a build from before `Batch.from_json` cannot read, and no third."""
    b = Batch(
        "1",
        "daniel-box",
        "/w1",
        "worktree-fanout-1",
        "u1",
        [1],
        "t",
        cited_paths=("scripts/a.py",),
        excused_paths=("docs/b.md",),
    )
    save(Manifest("20260101T000010Z", "worktree-a", [b]), root=tmp_path)
    (entry,) = json.loads(path("20260101T000010Z", tmp_path).read_text())["batches"]
    assert set(entry) - CONSTRUCTOR_READER_FIELDS == {"cited_paths", "excused_paths"}
    assert load("20260101T000010Z", tmp_path).batches == [b]


def test_runs_shows_a_parked_batch_with_its_reason_in_place_of_standing(
    tmp_path, capsys
):
    b1 = Batch("1", "daniel-box", "/w1", "worktree-fanout-1", "u1", [1], "t")
    b2 = Batch("2", "daniel-box", "/w2", "worktree-fanout-2", "u2", [2], "t")
    save(Manifest("20260101T000010Z", "worktree-a", [b1, b2]), root=tmp_path)
    report = json.dumps(
        {"type": "result", "result": "parked: refused before merge (a hold)\nPR"}
    )
    exited = "ActiveState=inactive\nResult=success\nExecMainStatus=0\n--- stderr\n--- report\n"
    tools, run = fake_tools()
    run.answers = {
        "daniel-box": ok(f"=== 1\n{exited}{report}\n=== 2\n{exited}"),
    }
    assert main(["runs", "--manifest-root", str(tmp_path)], tools) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "1 parked, 1 standing"
    assert "1 on daniel-box: worktree-fanout-1 #1 parked: refused before merge" in out
    assert "2 on daniel-box: worktree-fanout-2 #2 standing" in out
