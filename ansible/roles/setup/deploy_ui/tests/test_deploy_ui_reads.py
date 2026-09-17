"""Readers behind the four panels. Each parser gets an accept and a reject case."""

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
 5200     1     1 bash ./scripts/deploy.sh --list-services
 6000     1    20 /x/python3 /opt/gitops-deploy/gitops_deploy.py
 6001  6000     2 git fetch
 7000     1     3 grep land.py
"""
HELD = {
    "/var/lock/server-git-tree.lock": {6000, 6001},
    "/var/lock/server-deploy-all.lock": {5000, 5001, 5002, 4322},
    "/var/lock/server-deploy-homepage.lock": {5000, 5001, 5002},
    "/var/lock/server-deploy-n8n.lock": {4322},
}


def _rows():
    return {r.pid: r for r in reads.runs(reads.parse_ps(PS), HELD)}


def test_parse_ps_keys_every_process_by_pid_is_clean():
    procs = reads.parse_ps(PS)
    assert procs[5001].ppid == 5000 and procs[5001].elapsed_s == 88
    assert len(procs) == 13


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


def test_runs_queued_deploy_holds_nothing_is_flagged():
    r = _rows()[5100]
    assert r.kind == "deploy" and r.locks == ()


def test_runs_bare_lock_holder_is_a_row_is_clean():
    """The GitOps tick matches no run pattern; its tree-lock hold still shows."""
    r = _rows()[6000]
    assert r.kind == "lock" and r.locks == ("server-git-tree.lock",)


def test_runs_ignore_grep_shells_and_list_services_is_flagged():
    assert not {412, 5200, 7000} & set(_rows())


def test_parse_fuser_pairs_each_held_path_with_its_pids_is_clean():
    text = "/var/lock/a.lock: 10 11\n/var/lock/c.lock:  12\n"
    assert reads.parse_fuser(text) == {
        "/var/lock/a.lock": {10, 11},
        "/var/lock/c.lock": {12},
    }


def test_parse_fuser_empty_holds_nothing_is_flagged():
    assert reads.parse_fuser("") == {}


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


TWO_PRS_PS = """\
 4400     1    12 uv run python scripts/deploy_tools/land.py --pr 1550 --since 72c1a41
 4412  4400    12 /home/ubuntu/server/.venv/bin/python3 scripts/deploy_tools/land.py --pr 1550 --since 72c1a41
 4500     1     9 uv run python scripts/deploy_tools/land.py --pr 1551 --since 72c1a41
 4512  4500     9 /home/ubuntu/server/.venv/bin/python3 scripts/deploy_tools/land.py --pr 1551 --since 72c1a41
"""


def test_runs_keep_landings_for_different_prs_apart_is_flagged():
    got = reads.runs(reads.parse_ps(TWO_PRS_PS), {})
    assert [(r.pid, r.pr) for r in got] == [(4400, "1550"), (4500, "1551")]
