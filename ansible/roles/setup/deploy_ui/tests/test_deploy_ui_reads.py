"""Readers behind the four panels. Each parser gets an accept and a reject case."""

import os

import deploy_ui_reads as reads

PS = """\
  412     1  9001 /usr/bin/bash
 4321   412   125 /home/ubuntu/server/.venv/bin/python3 scripts/deploy_tools/land.py --pr 1543 --since 72c1a41b
 4322  4321    60 bash ./scripts/deploy.sh --at 72c1a41b --tags n8n
 4400     1    12 uv run python scripts/deploy_tools/land.py --pr 1550 --since abc
 4401  4400    11 /x/python3 scripts/deploy_tools/land.py --pr 1550 --since abc
 5000     1    90 bash ./scripts/deploy.sh --tags homepage
 5001  5000    88 uv run ansible-playbook ansible/deploy.yml --tags homepage
 5002  5001    87 /x/python3 /x/ansible-playbook ansible/deploy.yml --tags homepage
 5100     1    30 bash ./scripts/deploy.sh --tags homepage
 5101  5100    29 flock -w 600 -E 75 11
 5200     1     1 bash ./scripts/deploy.sh --list-services
 6000     1    20 /x/python3 /opt/gitops-deploy/gitops_deploy.py
 6001  6000     2 git fetch
 7000     1     3 grep land.py
"""
# fuser: who has each lock file OPEN. The queued deploy (5100) has homepage.lock open
# exactly like the holder, and its blocked `flock` child (5101) is in /proc/locks.
OPENED = {
    "/var/lock/server-git-tree.lock": {6000, 6001},
    "/var/lock/server-deploy-all.lock": {5000, 5001, 5002, 4322},
    "/var/lock/server-deploy-homepage.lock": {5000, 5001, 5002, 5100, 5101},
    "/var/lock/server-deploy-n8n.lock": {4322},
}
LOCKS = {
    "/var/lock/server-git-tree.lock": reads.FileLock(True, frozenset()),
    "/var/lock/server-deploy-all.lock": reads.FileLock(True, frozenset()),
    "/var/lock/server-deploy-homepage.lock": reads.FileLock(True, frozenset({5101})),
    "/var/lock/server-deploy-n8n.lock": reads.FileLock(True, frozenset()),
}


def _rows():
    return {r.pid: r for r in reads.runs(reads.parse_ps(PS), OPENED, LOCKS)}


def test_parse_ps_keys_every_process_by_pid_is_clean():
    procs = reads.parse_ps(PS)
    assert procs[5001].ppid == 5000 and procs[5001].elapsed_s == 88
    assert len(procs) == 14


def test_runs_fold_each_family_to_its_root_is_clean():
    """One row per landing and per deploy, whatever the wrapper depth."""
    assert set(_rows()) == {4321, 4400, 5000, 5100, 6000}


def test_runs_landing_owns_the_deploy_it_spawned_is_clean():
    r = _rows()[4321]
    assert r.kind == "land" and r.pr == "1543"
    # The locks its deploy.sh child holds are the landing's.
    assert r.locks == ("server-deploy-all.lock", "server-deploy-n8n.lock")


def test_runs_deploy_reads_tag_and_locks_is_clean():
    r = _rows()[5000]
    assert r.kind == "deploy" and r.tag == "homepage" and r.pr == ""
    assert r.locks == ("server-deploy-all.lock", "server-deploy-homepage.lock")


def test_runs_queued_deploy_is_waiting_not_holding_is_flagged():
    """The file is open in both families; only the kernel's waiter line separates them."""
    r = _rows()[5100]
    assert r.kind == "deploy" and r.locks == ()
    assert r.waiting_on == ("server-deploy-homepage.lock",)
    assert _rows()[5000].waiting_on == ()


def test_runs_open_but_ungranted_file_is_not_held_is_flagged():
    """fuser alone would call this a holder; without a granted lock it holds nothing."""
    rows = {r.pid: r for r in reads.runs(reads.parse_ps(PS), OPENED, {})}
    assert rows[5000].locks == () and rows[5000].waiting_on == ()


def test_runs_bare_lock_holder_is_a_row_is_clean():
    """The GitOps tick matches no run pattern; its tree-lock hold still shows."""
    r = _rows()[6000]
    assert r.kind == "lock" and r.locks == ("server-git-tree.lock",)


def test_runs_ignore_grep_shells_and_list_services_is_flagged():
    assert not {412, 5200, 7000} & set(_rows())


# The same queued deploy after #2412: the shim has exec'd `uv run … deploy_run.py`, which
# stays the family root (measured 2026-09-24: `uv run` spawns, it does not exec), and its
# python child blocks in flock(2) itself. A `--detach` run's forked child calls setsid, so
# it is reparented to 1 and is its own family root, holding the service lock alone.
PORTED_PS = """\
 8000     1    30 uv run --project /s/scripts/.. python /s/scripts/deploy_tools/deploy_run.py --tags n8n
 8001  8000    29 /s/.venv/bin/python /s/scripts/deploy_tools/deploy_run.py --tags n8n
 8200     1   300 /s/.venv/bin/python /s/scripts/deploy_tools/deploy_run.py --detach --tags sonarr
 8100     1     2 grep deploy_run.py
"""
PORTED_N8N = "/var/lock/server-deploy-n8n.lock"
PORTED_SONARR = "/var/lock/server-deploy-sonarr.lock"


def test_runs_ported_deploy_is_one_waiting_row_is_clean():
    rows = reads.runs(
        reads.parse_ps(PORTED_PS),
        {PORTED_N8N: {8001}, PORTED_SONARR: {8200}},
        {
            PORTED_N8N: reads.FileLock(True, frozenset({8001})),
            PORTED_SONARR: reads.FileLock(True, frozenset()),
        },
    )
    by_pid = {r.pid: r for r in rows}
    assert [(r.pid, r.kind, r.tag) for r in rows] == [
        (8000, "deploy", "n8n"),
        (8200, "deploy", "sonarr"),
    ]
    assert by_pid[8000].waiting_on == ("server-deploy-n8n.lock",)
    assert by_pid[8200].locks == ("server-deploy-sonarr.lock",)


def test_runs_ported_deploy_ignores_a_grep_for_it_is_flagged():
    rows = reads.runs(reads.parse_ps(PORTED_PS), {}, {})
    assert 8100 not in {r.pid for r in rows}


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


def test_read_state_missing_reads_clear_is_clean(state_dir):
    assert reads.read_state(state_dir)["hold_sha"] == ""


def test_read_state_present_reads_value_is_flagged(state_dir):
    (state_dir / "hold_sha").write_text("deadbeef\n")
    (state_dir / "staging_gate_override").write_text("")
    st = reads.read_state(state_dir)
    assert st["hold_sha"] == "deadbeef"
    assert st["staging_gate_override"] == "set"


def test_read_state_splits_hold_plane_into_its_entries_is_clean(state_dir):
    """One entry per failed apply since #2381, and the page lists them before a Clear (#2453)."""
    (state_dir / "hold_plane").write_text(
        "ansible/initial_setup.yml k3s; ansible/deploy.yml sonarr\n"
    )
    assert reads.read_state(state_dir)["hold_plane_entries"] == [
        "ansible/initial_setup.yml k3s",
        "ansible/deploy.yml sonarr",
    ]


def test_read_state_with_no_hold_plane_lists_no_entry_is_flagged(state_dir):
    assert reads.read_state(state_dir)["hold_plane_entries"] == []


def test_hold_plane_sep_matches_the_deployers_own_separator():
    """The oracle for the literal in `deploy_ui_reads`.

    The daemon runs outside the repo venv and cannot import `deploy_git`, so the separator and
    its parser are copied. pytest CAN import both: a change to how the deployer joins entries
    fails here rather than leaving the page showing one run-on entry.
    """
    import deploy_git

    held = "ansible/initial_setup.yml k3s; ansible/deploy.yml sonarr"
    assert reads.HOLD_PLANE_SEP == deploy_git.HOLD_PLANE_SEP
    assert reads.hold_plane_entries(held) == deploy_git.hold_plane_entries(held)


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


TWO_PRS_PS = """\
 4400     1    12 uv run python scripts/deploy_tools/land.py --pr 1550 --since 72c1a41
 4412  4400    12 /home/ubuntu/server/.venv/bin/python3 scripts/deploy_tools/land.py --pr 1550 --since 72c1a41
 4500     1     9 uv run python scripts/deploy_tools/land.py --pr 1551 --since 72c1a41
 4512  4500     9 /home/ubuntu/server/.venv/bin/python3 scripts/deploy_tools/land.py --pr 1551 --since 72c1a41
"""


def test_runs_keep_landings_for_different_prs_apart_is_flagged():
    got = reads.runs(reads.parse_ps(TWO_PRS_PS), {}, {})
    assert [(r.pid, r.pr) for r in got] == [(4400, "1550"), (4500, "1551")]
