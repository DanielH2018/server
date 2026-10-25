"""The per-user record of landing verdicts that `probe.py landing-verdicts` reads.

Run: uv run pytest scripts/deploy_tools/tests/test_land_verdict_log.py
"""

from deploy_tools.land_lib import verdict_log


def test_a_record_reads_back_as_the_newest_line_of_its_pr(tmp_path):
    path = tmp_path / "state" / "landing-verdicts.jsonl"
    verdict_log.record("5", 1, "VERDICT: ci-red (PR #5)", path)
    verdict_log.record("5", 0, "VERDICT: settled (PR #5)", path)
    assert [(e["pr"], e["rc"], e["line"]) for e in verdict_log.latest(path)] == [
        ("5", 0, "VERDICT: settled (PR #5)")
    ]


def test_a_missing_file_is_no_records(tmp_path):
    assert verdict_log.latest(tmp_path / "absent.jsonl") == []


def test_a_torn_line_does_not_hide_the_other_records(tmp_path):
    path = tmp_path / "landing-verdicts.jsonl"
    verdict_log.record("6", 0, "VERDICT: settled (PR #6)", path)
    with path.open("a") as fh:
        fh.write('{"pr": "7", "at": \n{"other": 1}\n')
    assert [e["pr"] for e in verdict_log.latest(path)] == ["6"]


def test_the_file_is_trimmed_to_the_newest_records(tmp_path):
    path = tmp_path / "landing-verdicts.jsonl"
    for pr in range(7):
        verdict_log.record(str(pr), 0, f"VERDICT: settled (PR #{pr})", path, keep=3)
    assert len(path.read_text().splitlines()) == 3
    assert {e["pr"] for e in verdict_log.latest(path)} == {"4", "5", "6"}


def test_the_env_var_overrides_the_default_path(tmp_path, monkeypatch):
    monkeypatch.setenv(verdict_log.PATH_ENV, str(tmp_path / "x.jsonl"))
    assert verdict_log.default_path() == tmp_path / "x.jsonl"
    monkeypatch.delenv(verdict_log.PATH_ENV)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert verdict_log.default_path() == tmp_path / "homelab" / "landing-verdicts.jsonl"
