"""Guards for the tick ledger the deployer writes and the backfill only reads.

Two properties matter beyond the loader working. The tick ledger must never reach the part-1
verdict — it measures a differently-scoped run, and `--since-ledger` plans its next window from
the newest row of the file it writes, so a tick row in that file would misplan the ratchet. And
the outcome vocabulary is duplicated across two trees that cannot import each other, so it is
asserted equal here rather than kept in step by hand.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from lib import yaml_fast

import backfill_staging_gate as bf

REPO = pathlib.Path(__file__).resolve().parents[3]
ROLE = REPO / "ansible/roles/setup/gitops_deploy"

sys.path.insert(0, str(ROLE / "files"))

import deploy_staging as ds  # noqa: E402


def _tick(outcome: str, at: str = "2026-09-02T22:00:00-05:00") -> dict:
    return {
        "at": at,
        "sha": "c0ffee1234",
        "tags": "freshrss",
        "verdict": "pass",
        "outcome": outcome,
    }


def _write(path: pathlib.Path, rows: list[dict]) -> pathlib.Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


# ── the vocabulary is one vocabulary, across two trees ───────────────────────────────────


def test_tick_and_backfill_agree_on_the_outcome_vocabulary():
    """Named members, not a count: a drift failure must say which word went missing."""
    assert {ds.TICK_OK, ds.TICK_FALSE_FAILURE, ds.TICK_NEEDS_TRIAGE} == {
        bf.OK,
        bf.FALSE_FAILURE,
        bf.NEEDS_TRIAGE,
    }


def test_every_outcome_a_tick_can_emit_is_one_the_backfill_defines():
    emitted = {
        ds.staging_tick_outcome(v)
        for v in (ds.STAGING_PASS, ds.STAGING_REJECTED, ds.STAGING_NO_VERDICT)
    }
    assert emitted <= {bf.OK, bf.FALSE_FAILURE, bf.TRUE_FAILURE, bf.NEEDS_TRIAGE}
    assert None not in emitted


def test_the_tick_ledger_constant_matches_the_ansible_default():
    """The deployer needs a module-level literal (the state_dir guard requires it), so the path
    exists in two places. Read the YAML rather than restating it in a third."""
    defaults = yaml_fast.safe_load((ROLE / "defaults/main.yml").read_text())
    literal = next(
        line.split('"')[1]
        for line in (ROLE / "files/gitops_deploy.py").read_text().splitlines()
        if line.startswith("STAGING_TICK_LEDGER = ")
    )
    assert literal == defaults["gitops_deploy_staging_tick_ledger"]


def test_the_two_ledgers_are_different_files():
    """The whole reason this is a separate ledger. If these ever became one path, --since-ledger
    would plan its window from a tick row."""
    defaults = yaml_fast.safe_load((ROLE / "defaults/main.yml").read_text())
    assert (
        defaults["gitops_deploy_staging_tick_ledger"]
        != defaults["gitops_deploy_staging_backfill_ledger"]
    )


def test_what_the_deployer_writes_is_what_the_backfill_reads(tmp_path, monkeypatch):
    """The one end-to-end tie. Every other test here builds the row by hand, so a field the
    recorder renamed would pass all of them and only fail on the host, an hour later, silently —
    `load_tick_ledger` skips a row it cannot construct rather than raising."""
    sys.path.insert(0, str(ROLE / "files"))
    import gitops_deploy as gd

    ledger = tmp_path / "ticks.jsonl"
    monkeypatch.setattr(gd, "STAGING_TICK_LEDGER", str(ledger))
    gd.record_staging_tick("c0ffee1234", {"freshrss"}, ds.STAGING_REJECTED)

    loaded = bf.load_tick_ledger(ledger)
    assert len(loaded) == 1, f"the recorder's row did not load: {ledger.read_text()!r}"
    assert loaded[0].outcome == bf.NEEDS_TRIAGE
    assert loaded[0].sha == "c0ffee1234"


# ── load_tick_ledger ─────────────────────────────────────────────────────────────────────


def test_a_missing_ledger_is_an_empty_ledger(tmp_path):
    assert bf.load_tick_ledger(tmp_path / "absent.jsonl") == []


def test_rows_load_oldest_first(tmp_path):
    path = _write(
        tmp_path / "t.jsonl",
        [_tick(bf.OK, at="2026-09-01T10:00:00-05:00"), _tick(bf.NEEDS_TRIAGE)],
    )
    loaded = bf.load_tick_ledger(path)
    assert [t.outcome for t in loaded] == [bf.OK, bf.NEEDS_TRIAGE]


def test_a_half_written_row_is_skipped_rather_than_fatal(tmp_path):
    """The reject half. The deployer writes this file on a path that may not raise, so a torn
    line is a reachable end state — the report must survive it and still count the good rows."""
    path = tmp_path / "t.jsonl"
    path.write_text(json.dumps(_tick(bf.OK)) + '\n{"at": "2026-09-0\n')
    loaded = bf.load_tick_ledger(path)
    assert len(loaded) == 1
    assert loaded[0].outcome == bf.OK


def test_a_row_with_unexpected_fields_is_skipped_rather_than_fatal(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text(json.dumps({"at": "x", "unexpected": 1}) + "\n")
    assert bf.load_tick_ledger(path) == []


# ── report_ticks ─────────────────────────────────────────────────────────────────────────


def test_an_empty_ledger_says_so_rather_than_printing_zeroes():
    assert bf.report_ticks([]) == ["  no real gated tick recorded yet"]


def test_a_rejection_is_called_out(tmp_path):
    path = _write(tmp_path / "t.jsonl", [_tick(bf.NEEDS_TRIAGE)])
    lines = bf.report_ticks(bf.load_tick_ledger(path))
    assert any(line.startswith("  ! 1 rejection(s)") for line in lines)


def test_a_clean_ledger_raises_no_flag(tmp_path):
    """The rejecting half of the pair above."""
    path = _write(tmp_path / "t.jsonl", [_tick(bf.OK), _tick(bf.OK)])
    lines = bf.report_ticks(bf.load_tick_ledger(path))
    assert not any(line.startswith("  !") for line in lines)
    assert any("ticks=2" in line for line in lines)


def test_the_scheduled_form_prints_the_tick_section(tmp_path, monkeypatch, capsys):
    """`main`'s wiring, which --help cannot reach: the report lines only print on a real run.

    The plan is stubbed empty, which is both the common hourly case — no new gateable commit —
    and the only way to exercise this without driving a staging deploy.
    """
    backfill = _write(
        tmp_path / "backfill.jsonl",
        [
            {
                "sha": "a" * 40,
                "subject": "s",
                "tags": "freshrss",
                "rc": 0,
                "outcome": bf.OK,
                "note": "",
            }
        ],
    )
    ticks = _write(tmp_path / "ticks.jsonl", [_tick(bf.NEEDS_TRIAGE)])
    monkeypatch.setattr(bf, "collect", lambda ref, count: [])
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "backfill_staging_gate.py",
            "--since-ledger",
            "--jsonl",
            str(backfill),
            "--tick-jsonl",
            str(ticks),
        ],
    )
    bf.main()
    out = capsys.readouterr().out
    assert "real gated ticks" in out
    assert "ticks=1" in out
    assert "! 1 rejection(s)" in out


def test_without_the_flag_no_tick_section_is_printed(tmp_path, monkeypatch, capsys):
    """The rejecting half: the section is opt-in, so the deployer-free caller sees nothing."""
    backfill = _write(
        tmp_path / "backfill.jsonl",
        [
            {
                "sha": "a" * 40,
                "subject": "s",
                "tags": "freshrss",
                "rc": 0,
                "outcome": bf.OK,
                "note": "",
            }
        ],
    )
    monkeypatch.setattr(bf, "collect", lambda ref, count: [])
    monkeypatch.setattr(
        sys,
        "argv",
        ["backfill_staging_gate.py", "--since-ledger", "--jsonl", str(backfill)],
    )
    bf.main()
    assert "real gated ticks" not in capsys.readouterr().out


# ── the tick ledger never reaches part 1 ─────────────────────────────────────────────────


@pytest.mark.parametrize("outcome", [bf.OK, bf.FALSE_FAILURE, bf.NEEDS_TRIAGE])
def test_part_1_is_decided_without_the_tick_ledger(outcome):
    """summarise and clean_streak take `Run`s only. A tick can neither extend the streak that
    arms the gate nor park it — that separation is the reason for two files."""
    runs = [
        bf.Run(sha="a" * 40, subject="s", tags="freshrss", rc=0, outcome=bf.OK, note="")
    ]
    verdict, reasons = bf.summarise(runs, required=1)
    assert verdict == "MET" and not reasons
    # Feeding the report a tick of any outcome changes nothing about the above.
    assert bf.report_ticks([bf.TickRun(**_tick(outcome))])
    assert bf.summarise(runs, required=1) == (verdict, reasons)
