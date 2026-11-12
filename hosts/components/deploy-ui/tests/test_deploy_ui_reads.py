"""Readers behind the four panels. Each parser gets an accept and a reject case."""

import os
from pathlib import PurePath


from deploy_ui_lib import deploy_ui_reads as reads
from lib.tree_lock import TREE_LOCK

PS = """\
  412     1  9001 /usr/bin/bash
 4321   412   125 /home/ubuntu/server/.venv/bin/python3 scripts/deploy_tools/land.py --pr 1543 --since 72c1a41b
 4322  4321    60 git fetch origin
 4400     1    12 uv run python scripts/deploy_tools/land.py --pr 1550 --since abc
 4401  4400    11 /x/python3 scripts/deploy_tools/land.py --pr 1550 --since abc
 4500     1    40 /x/python3 scripts/deploy_tools/land.py --tags homepage
 5000     1    90 uv run ansible-playbook ansible/deploy.yml --tags homepage
 5001  5000    88 /x/python3 /x/ansible-playbook ansible/deploy.yml --tags homepage
 7000     1     3 grep land.py
"""
# fuser: who has the tree lock OPEN. The second landing (4400) has it open exactly like the
# holder, and its python child (4401) is blocked on it in /proc/locks.
OPENED = {TREE_LOCK: {4321, 4322, 4401}}
LOCKS = {TREE_LOCK: reads.FileLock(True, frozenset({4401}))}
TREE_LOCK_NAME = PurePath(TREE_LOCK).name


def _rows():
    return {r.pid: r for r in reads.runs(reads.parse_ps(PS), OPENED, LOCKS)}


def test_parse_ps_keys_every_process_by_pid_is_clean():
    procs = reads.parse_ps(PS)
    assert procs[5001].ppid == 5000 and procs[5001].elapsed_s == 88
    assert len(procs) == 9


def test_runs_fold_each_family_to_its_root_is_clean():
    """One row per landing, whatever the wrapper depth."""
    assert set(_rows()) == {4321, 4400, 4500}


def test_runs_landing_holds_the_tree_lock_its_child_has_open_is_clean():
    r = _rows()[4321]
    assert r.kind == "land" and r.pr == "1543"
    assert r.locks == (TREE_LOCK_NAME,)


def test_runs_landing_by_tags_reads_its_tag_is_clean():
    r = _rows()[4500]
    assert r.kind == "land" and r.tag == "homepage" and r.pr == ""


def test_runs_playbook_holding_no_lock_is_not_a_row_is_flagged():
    assert not {5000, 5001} & set(_rows())


def test_runs_queued_landing_is_waiting_not_holding_is_flagged():
    """The file is open in both families; only the kernel's waiter line separates them."""
    r = _rows()[4400]
    assert r.kind == "land" and r.locks == ()
    assert r.waiting_on == (TREE_LOCK_NAME,)
    assert _rows()[4321].waiting_on == ()


def test_runs_open_but_ungranted_file_is_not_held_is_flagged():
    """fuser alone would call this a holder; without a granted lock it holds nothing."""
    rows = {r.pid: r for r in reads.runs(reads.parse_ps(PS), OPENED, {})}
    assert rows[4321].locks == () and rows[4321].waiting_on == ()


def test_runs_bare_lock_holder_is_a_row_is_clean():
    """A holder that matches no run pattern still shows by its tree-lock hold."""
    ps = " 6000     1    20 /x/python3 /opt/homelab/other_holder.py\n 6001  6000     2 git fetch\n"
    rows = reads.runs(
        reads.parse_ps(ps),
        {TREE_LOCK: {6000, 6001}},
        {TREE_LOCK: reads.FileLock(True, frozenset())},
    )
    assert [(r.pid, r.kind, r.locks) for r in rows] == [
        (6000, "lock", (TREE_LOCK_NAME,))
    ]


def test_runs_ignore_grep_shells_is_flagged():
    assert not {412, 7000} & set(_rows())


def test_parse_fuser_pairs_each_held_path_with_its_pids_is_clean():
    text = "/var/lock/a.lock: 10 11\n/var/lock/c.lock:  12\n"
    assert reads.parse_fuser(text) == {
        "/var/lock/a.lock": {10, 11},
        "/var/lock/c.lock": {12},
    }


def test_parse_fuser_empty_holds_nothing_is_flagged():
    assert reads.parse_fuser("") == {}


PROC_LOCKS = """\
4: FLOCK  ADVISORY  WRITE 25343 41:10:13 0 EOF
56: FLOCK  ADVISORY  WRITE 1930784 fc:00:6564011 0 EOF
56: -> FLOCK  ADVISORY  WRITE 1930788 fc:00:6564011 0 EOF
57: POSIX  ADVISORY  WRITE 1620 00:1b:1915 0 EOF
58: FLOCK  ADVISORY  READ 1917491 00:1d:2364212 0 EOF
"""


def test_parse_proc_locks_reads_granted_and_waiting_is_clean():
    """Measured shape: a `flock` holder and a second `flock -w` blocked on the same file."""
    got = reads.parse_proc_locks(PROC_LOCKS)
    assert got[(0xFC, 0, 6564011)] == reads.FileLock(True, frozenset({1930788}))
    assert got[(0, 0x1D, 2364212)] == reads.FileLock(True, frozenset())


def test_parse_proc_locks_ignores_posix_locks_is_flagged():
    assert (0, 0x1B, 1915) not in reads.parse_proc_locks(PROC_LOCKS)


def test_lock_key_matches_proc_locks_identity_is_clean(tmp_path):
    p = tmp_path / "x.lock"
    p.touch()
    st = p.stat()
    assert reads.lock_key(str(p)) == (
        os.major(st.st_dev),
        os.minor(st.st_dev),
        st.st_ino,
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


TWO_PRS_PS = """\
 4400     1    12 uv run python scripts/deploy_tools/land.py --pr 1550 --since 72c1a41
 4412  4400    12 /home/ubuntu/server/.venv/bin/python3 scripts/deploy_tools/land.py --pr 1550 --since 72c1a41
 4500     1     9 uv run python scripts/deploy_tools/land.py --pr 1551 --since 72c1a41
 4512  4500     9 /home/ubuntu/server/.venv/bin/python3 scripts/deploy_tools/land.py --pr 1551 --since 72c1a41
"""


def test_runs_keep_landings_for_different_prs_apart_is_flagged():
    got = reads.runs(reads.parse_ps(TWO_PRS_PS), {}, {})
    assert [(r.pid, r.pr) for r in got] == [(4400, "1550"), (4500, "1551")]
