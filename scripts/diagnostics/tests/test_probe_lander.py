"""Tests for `probe.py lander`.

The pass lines and landing logs are the ones daniel-box held on 2026-10-25, in the shape
`host_reader.READER` returns them.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from diagnostics.probe_lib import lander
from fanout_lib import autoland
from lib.exit_codes import PROBE_FAILED, PROBE_OK

NOW = 1792959300.0
PERIOD, CEILING, LOG_DIR = lander.settings()

OLD_PASS = [
    [1792954393.0, 5, "p1", "uv", "40 PRs in the last records, 2 still open"],
    [1792954393.0, 5, "p1", "uv", "#6319 batch 6128: eligible, handed off to land.sh; log in /x"],
    [1792954393.0, 5, "p1", "uv", "#6300 batch 6101: ineligible, the head moved since the review (50fcf33e -> 0d086888)"],
]  # fmt: skip
NEW_PASS_AT = 1792958995.0
NEW_PASS = [
    [1792958995.0, 5, "p2", "uv", "52 PRs in the last records, 2 still open"],
    [1792958995.0, 5, "p2", "uv", "#6333 batch 6327: ineligible, it changes the lander's own surface: .claude/settings.json"],
    [1792958995.0, 5, "p2", "uv", "#6335 batch 6325: ineligible, the head moved since the review (b2c4c6ce -> 8a098e80)"],
    [1792958996.0, 6, "p2", "systemd", "claude-fanout-server-lander.service: Deactivated successfully."],
]  # fmt: skip
LOGS = {
    "status": "ok",
    "rows": [
        {
            "name": "land6319-20261025-192315.log",
            "verdict": "VERDICT: settled (PR #6319, 11dc8e28, tags: longhorn-restore-drill)",
            "rc": "0\n",
            "alive": False,
        },
        {
            "name": "land6321-20261025-200000.log",
            "verdict": None,
            "rc": None,
            "alive": True,
        },
    ],
}


def _doc(entries=None, result="success", load="loaded", logs=LOGS):
    return {
        "host": "daniel-box",
        "user": "ubuntu",
        "journal": {
            "lander": {"rc": 0, "stderr": "", "entries": entries or OLD_PASS + NEW_PASS}
        },
        "show": {lander.UNIT: {"LoadState": load, "ActiveState": "inactive", "Result": result}},
        "logs": logs,
    }  # fmt: skip


def _report(doc, pr=None, now=NOW):
    return lander.report(doc, now=now, period=PERIOD, ceiling=CEILING, pr=pr)


def test_the_newest_pass_is_the_queue_and_an_older_line_is_the_pr_s_last_action():
    text, code = _report(_doc())
    assert code == PROBE_OK
    queue = text.split("landings started")[0]
    assert "#6335 batch 6325: ineligible, the head moved" in queue
    assert "#6300" not in queue
    others = text.split("other PRs the window named")[1]
    assert "#6300 batch 6101: ineligible" in others
    assert "#6335" not in others


def test_each_landing_shows_whether_it_runs_its_rc_and_its_verdict():
    text, _ = _report(_doc())
    assert "#6319  land6319-20261025-192315.log  ended rc 0  VERDICT: settled" in text
    assert "#6321  land6321-20261025-200000.log  running  no VERDICT line yet" in text


def test_a_failed_newest_run_is_the_fail_verdict():
    text, code = _report(_doc(result="exit-code"))
    assert code == PROBE_FAILED
    assert "the newest run ended exit-code" in text


def test_a_pass_older_than_two_periods_and_the_ceiling_is_stale():
    text, code = _report(_doc(), now=NEW_PASS_AT + 2 * PERIOD + CEILING + 1)
    assert code == PROBE_FAILED
    assert "the newest pass is" in text


def test_a_pass_that_could_not_read_github_is_the_fail_verdict():
    unread = [
        [1792958995.0, 5, "p3", "uv", "GitHub unreadable, nothing landed: HTTP 502"]
    ]
    text, code = _report(_doc(entries=OLD_PASS + unread))
    assert code == PROBE_FAILED
    assert "verdict: GitHub unreadable, nothing landed: HTTP 502" in text


def test_a_disarmed_lander_says_so():
    text, code = _report(_doc(load="not-found"))
    assert code == PROBE_FAILED
    assert "is not-found on daniel-box" in text


def test_unreadable_landing_logs_are_named_and_leave_the_journal_verdict():
    """The agent user cannot list the operator's home on daniel-box."""
    logs = {"status": "unreadable", "error": "[Errno 13] Permission denied"}
    text, code = _report(_doc(logs=logs))
    assert code == PROBE_OK
    assert "landing logs unreadable: [Errno 13] Permission denied" in text


def test_pr_narrows_every_section_to_that_pr():
    text, _ = _report(_doc(), pr=6319)
    assert "#6319 batch 6128: eligible, handed off" in text
    assert "land6319-" in text
    assert "#6335" not in text and "land6321-" not in text


def test_the_settings_come_from_the_role_and_the_inventory():
    assert PERIOD == 600 and CEILING == 1800
    assert LOG_DIR == "/home/ubuntu/.local/state/fanout-server-lander/logs"


def test_a_pass_that_could_not_read_daniel_server_is_the_fail_verdict():
    """The line `autoland.run` prints, host first; that pass exits 0, so systemd says success."""
    line = f"{autoland.SERVER_HOST} records unreadable, nothing landed: exit 255: ssh refused"
    text, code = _report(_doc(entries=[*OLD_PASS, [1792958995.0, 5, "p3", "uv", line]]))
    assert code == PROBE_FAILED
    assert f"verdict: {line}" in text
