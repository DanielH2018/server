"""The waiting gauge `fanout.py land-server` writes (#5671): what counts as waiting on a person.

The reason table is driven through the real `autoland.problems`, so a rewording of a reason
there fails here rather than silently dropping that reason to zero.

Run: uv run pytest scripts/dev/tests/test_fanout_operator_waits.py
"""

import json
import re
import subprocess

from _fanout_fakes import fake_tools
from test_fanout_autoland import _pr, _record

from fanout_lib import autoland, operator_waits
from fanout_lib.autoland import Boundaries

# 2026-10-18T01:00:00Z, the stamp `_record` puts in every record's file name.
SAVED = 1792285200.0
NOW = SAVED + 3600
SAMPLE = re.compile(
    r"^(homelab_awaiting_operator(_oldest_seconds)?\{reason|findings_issues\{state"
    r'|fanout_batch_capacity\{host)="[a-z-]+"\} \d+$'
)


def _issue(number, *labels):
    return {"number": number, "labels": [{"name": n} for n in ("claude", *labels)]}


def _no_issues():
    return []


def _gauge(tmp_path, server, prs, box=(), owed="", ssh=None, issues=_no_issues):
    answer = ssh or subprocess.CompletedProcess([], 0, json.dumps(server), "")
    tools, _ = fake_tools(answers={autoland.SERVER_HOST: answer})
    b = Boundaries(
        tools=tools,
        open_prs=lambda: prs,
        prepare=lambda *a: tmp_path,
        start=lambda *a: subprocess.CompletedProcess([], 0, "", ""),
        release=lambda *a: None,
        running=lambda n: False,
        local_records=lambda: list(box),
        owed=lambda: owed,
        open_issues=issues,
        clock=lambda: NOW,
    )
    gauge = tmp_path / "textfile" / "fanout_operator_waits.prom"
    gauge.parent.mkdir(exist_ok=True)
    lines = []
    autoland.run(
        b, tmp_path / "primary", tmp_path / "state", out=lines.append, gauge=gauge
    )
    return gauge, "\n".join(lines)


def _count(text, reason):
    found = re.search(
        rf'^homelab_awaiting_operator\{{reason="{reason}"\}} (\d+)$', text, re.M
    )
    assert found, f"no sample for reason={reason}: {text}"
    return int(found[1])


def test_every_waiting_reason_is_named_by_the_text_problems_returns():
    """The named members: each reason a person must clear, reached through `problems` itself."""
    cases = {
        "lander-surface": (
            _record(),
            _pr(files=[{"path": ".github/workflows/ci.yml"}]),
        ),
        "operator-step": (_record(), _pr(body="## Before merge\n- [ ] run the diff\n")),
        "findings-remain": (_record(remaining=2), _pr()),
        "needs-input": (_record(outcome="needs-input"), _pr()),
    }
    for reason, (rec, pr) in cases.items():
        assert operator_waits.reason_of(autoland.problems(rec, pr)) == reason, reason


def test_a_reason_that_waits_on_the_pipeline_is_not_counted():
    for rec, pr in (
        (_record(), _pr(headRefOid="b" * 40)),
        (_record(), _pr(isDraft=True)),
        (_record(head=""), _pr()),
        (_record(), _pr()),
    ):
        assert operator_waits.reason_of(autoland.problems(rec, pr)) == ""


def test_a_held_server_pr_counts_with_its_age_from_the_record(tmp_path):
    gauge, _ = _gauge(
        tmp_path,
        [_record(5001, outcome="needs-input")],
        [_pr(5001)],
    )
    text = gauge.read_text()
    assert _count(text, "needs-input") == 1
    assert 'homelab_awaiting_operator_oldest_seconds{reason="needs-input"} 3600' in text
    assert all(
        SAMPLE.match(line) for line in text.splitlines() if not line.startswith("#")
    )
    assert _count(text, "lander-surface") == 0


def test_a_wait_ends_when_its_pr_is_no_longer_open(tmp_path):
    gauge, _ = _gauge(tmp_path, [_record(5001, outcome="needs-input")], [])
    assert _count(gauge.read_text(), "needs-input") == 0


def test_a_box_batch_counts_only_what_holds_its_own_landing(tmp_path):
    box = [
        _record(6001, outcome="needs-input"),
        _record(6002),
    ]
    prs = [_pr(6001), _pr(6002, files=[{"path": ".claude/settings.json"}])]
    gauge, _ = _gauge(tmp_path, [], prs, box=box)
    text = gauge.read_text()
    assert _count(text, "needs-input") == 1
    assert _count(text, "lander-surface") == 0


def test_an_owed_manual_plane_counts_and_a_torn_line_is_skipped(tmp_path):
    owed = "\n".join(
        [
            json.dumps(
                {
                    "class": "manual_plane",
                    "subject": "k3s",
                    "origin": "abc",
                    "at": int(NOW - 600),
                }
            ),
            json.dumps(
                {"class": "k8s_deferred", "subject": "x", "origin": "abc", "at": 1}
            ),
            '{"class": "manual_plane", "at"',
        ]
    )
    gauge, _ = _gauge(tmp_path, [], [], owed=owed)
    text = gauge.read_text()
    assert _count(text, "manual-plane") == 1
    assert 'homelab_awaiting_operator_oldest_seconds{reason="manual-plane"} 600' in text


def test_an_unreadable_daniel_server_writes_no_gauge(tmp_path):
    gauge, text = _gauge(
        tmp_path, [], [_pr()], ssh=subprocess.CompletedProcess([], 255, "", "no route")
    )
    assert "records unreadable" in text
    assert not gauge.exists()


def test_a_dry_run_writes_no_gauge(tmp_path):
    tools, _ = fake_tools(
        answers={autoland.SERVER_HOST: subprocess.CompletedProcess([], 0, "[]", "")}
    )
    b = Boundaries(
        tools=tools, open_prs=lambda: [], local_records=list, owed=str, open_issues=list
    )
    gauge = tmp_path / "fanout_operator_waits.prom"
    autoland.run(b, tmp_path, tmp_path, dry_run=True, out=lambda _: None, gauge=gauge)
    assert not gauge.exists()


def test_the_written_file_is_world_readable(tmp_path):
    gauge, _ = _gauge(tmp_path, [], [])
    assert gauge.stat().st_mode & 0o777 == 0o644


def test_each_issue_lands_in_one_state_and_manual_wins():
    issues = [
        _issue(1),
        _issue(2, "claimed"),
        _issue(3, "not-before:2026-10-20"),
        _issue(4, "not-before:2026-10-17"),
        _issue(5, "manual", "claimed"),
        _issue(6, "manual", "not-before:2026-10-20"),
    ]
    assert operator_waits.issue_states(issues, "2026-10-18") == {
        "free": 2,
        "claimed": 1,
        "deferred": 1,
        "manual": 2,
    }


def test_the_pass_writes_the_queue_and_each_hosts_capacity(tmp_path):
    gauge, _ = _gauge(
        tmp_path, [], [], issues=lambda: [_issue(1), _issue(2, "claimed")]
    )
    text = gauge.read_text()
    assert 'findings_issues{state="free"} 1' in text
    assert 'findings_issues{state="claimed"} 1' in text
    for host in (autoland.LOCAL_HOST, autoland.SERVER_HOST):
        assert f'fanout_batch_capacity{{host="{host}"}} 3' in text


def test_a_failed_issue_read_keeps_the_waits_and_drops_the_queue(tmp_path):
    def broken():
        raise OSError("gh: HTTP 502")

    gauge, text = _gauge(
        tmp_path, [_record(5001, outcome="needs-input")], [_pr(5001)], issues=broken
    )
    body = gauge.read_text()
    assert "issue queue not read: gh: HTTP 502" in text
    assert _count(body, "needs-input") == 1
    assert "findings_issues" not in body
