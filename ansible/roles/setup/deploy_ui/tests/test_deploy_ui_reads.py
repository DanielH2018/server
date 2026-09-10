"""Readers behind the four panels. Each parser gets an accept and a reject case."""

import deploy_ui_reads as reads

PS = """\
  412  9001 /usr/bin/bash
 4321   125 /home/ubuntu/server/.venv/bin/python3 scripts/deploy_tools/land.py --pr 1543 --since 72c1a41b
 4400    12 uv run python scripts/deploy_tools/land.py --pr 1550 --since abc
 5000     3 grep land.py
"""


def test_parse_ps_finds_land_processes_is_clean():
    got = reads.parse_ps(PS)
    assert [(l.pid, l.elapsed_s, l.pr) for l in got] == [
        (4321, 125, "1543"),
        (4400, 12, "1550"),
    ]


def test_parse_ps_ignores_grep_and_shells_is_flagged():
    assert all(l.pid not in (412, 5000) for l in reads.parse_ps(PS))


def test_parse_fuser_pid_lowest_is_clean():
    assert reads.parse_fuser_pid("  4400  4321\n") == 4321


def test_parse_fuser_pid_empty_is_flagged():
    assert reads.parse_fuser_pid("") is None


def test_read_state_missing_reads_clear_is_clean(state_dir):
    assert reads.read_state(state_dir)["hold_sha"] == ""


def test_read_state_present_reads_value_is_flagged(state_dir):
    (state_dir / "hold_sha").write_text("deadbeef\n")
    (state_dir / "staging_gate_override").write_text("")
    st = reads.read_state(state_dir)
    assert st["hold_sha"] == "deadbeef"
    assert st["staging_gate_override"] == "set"


def test_parse_stale_lines_is_clean():
    text = "homepage: roles/k8s/homepage changed in 5aab47af\nn8n: no release record"
    assert reads.parse_stale(text) == [
        {"service": "homepage", "reason": "roles/k8s/homepage changed in 5aab47af"},
        {"service": "n8n", "reason": "no release record"},
    ]


def test_parse_stale_none_stale_is_flagged():
    assert (
        reads.parse_stale(
            "0 service(s) stale; every known k8s service has a current record."
        )
        == []
    )


PRS = """[{"number": 1550, "title": "T", "headRefName": "b", "isDraft": false,
 "statusCheckRollup": [{"conclusion": "SUCCESS"}, {"conclusion": "SUCCESS"}]},
 {"number": 1551, "title": "U", "headRefName": "c", "isDraft": true,
 "statusCheckRollup": [{"conclusion": "FAILURE"}, {"status": "IN_PROGRESS"}]},
 {"number": 1552, "title": "V", "headRefName": "d", "isDraft": false, "statusCheckRollup": []}]"""


def test_parse_prs_rollup_is_clean():
    got = {p["number"]: p["ci"] for p in reads.parse_prs(PRS)}
    assert got == {1550: "pass", 1551: "fail", 1552: "none"}


def test_parse_prs_pending_without_failure_is_flagged():
    raw = '[{"number": 1, "title": "", "headRefName": "", "isDraft": false, "statusCheckRollup": [{"status": "IN_PROGRESS"}]}]'
    assert reads.parse_prs(raw)[0]["ci"] == "pending"
