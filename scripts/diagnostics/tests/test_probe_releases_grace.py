"""The grace window on `probe.py releases --stale-only` -- a fresh merge is pending, not stale.

Split from `test_probe_releases_stale.py`, which covers WHICH paths invalidate a stamp; this
file covers WHEN a change starts to count. `--grace-minutes` exists because the monitor
pushed DOWN for a merge whose own landing was mid-deploy (code-server, 2026-09-21).

Run: uv run pytest scripts/diagnostics/tests/test_probe_releases_grace.py
"""

import subprocess

from diagnostics.probe_lib import releases as pr

from _release_fixtures import (
    _GIT_CLEAN_ENV,
    _commit,
    _init_repo,
    _record,
    _set_origin_master,
)


def _committer_time(repo, sha):
    out = subprocess.run(
        ["git", "show", "-s", "--format=%ct", sha],
        cwd=repo,
        env=_GIT_CLEAN_ENV,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return int(out.strip())


def _stale_role_repo(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    base = _commit(
        repo, {"ansible/roles/k8s/sonarr/templates/deployment.yaml.j2": "v1\n"}, "v1"
    )
    tip = _commit(
        repo, {"ansible/roles/k8s/sonarr/templates/deployment.yaml.j2": "v2\n"}, "v2"
    )
    _set_origin_master(repo, tip)
    return repo, base, tip


def test_a_merge_inside_the_grace_window_is_pending_not_stale(tmp_path):
    repo, base, tip = _stale_role_repo(tmp_path)
    pending = {}
    stale = pr.compute_stale(
        [_record("sonarr", commit=base)],
        repo_root=repo,
        shared_roles=set(),
        grace_seconds=3600,
        now=_committer_time(repo, tip) + 600,
        pending=pending,
    )
    assert stale == {}
    assert pending == {"sonarr": 600}


def test_a_merge_older_than_the_grace_window_is_flagged(tmp_path):
    repo, base, tip = _stale_role_repo(tmp_path)
    pending = {}
    stale = pr.compute_stale(
        [_record("sonarr", commit=base)],
        repo_root=repo,
        shared_roles=set(),
        grace_seconds=3600,
        now=_committer_time(repo, tip) + 3601,
        pending=pending,
    )
    assert "sonarr" in stale
    assert pending == {}


def _commit_dated(repo, files, message, epoch):
    """`_commit`, with the committer clock pinned so a test can build an old commit."""
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    env = {
        **_GIT_CLEAN_ENV,
        "GIT_AUTHOR_DATE": f"@{epoch} +0000",
        "GIT_COMMITTER_DATE": f"@{epoch} +0000",
    }
    subprocess.run(["git", "add", "-A"], cwd=repo, env=env, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", message], cwd=repo, env=env, check=True
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_a_fresh_commit_on_old_drift_does_not_restart_the_window(tmp_path):
    """The window counts from the OLDEST un-applied change, not the newest.

    `roles/k8s/manifests/` is an offending path for the whole fleet, so dating drift by the
    newest commit would let one busy shared role keep every service pending forever.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    template = "ansible/roles/k8s/sonarr/templates/deployment.yaml.j2"
    base = _commit(repo, {template: "v1\n"}, "v1")
    fresh = _committer_time(repo, base)
    _commit_dated(repo, {template: "v2\n"}, "v2, two hours ago", fresh - 7200)
    tip = _commit_dated(repo, {template: "v3\n"}, "v3, two minutes ago", fresh - 120)
    _set_origin_master(repo, tip)

    pending = {}
    stale = pr.compute_stale(
        [_record("sonarr", commit=base)],
        repo_root=repo,
        shared_roles=set(),
        grace_seconds=3600,
        now=fresh,
        pending=pending,
    )
    assert "sonarr" in stale
    assert pending == {}


def test_the_default_grace_is_none(tmp_path):
    """The cron passes the window explicitly; a caller that does not gets the old contract."""
    repo, base, tip = _stale_role_repo(tmp_path)
    stale = pr.compute_stale(
        [_record("sonarr", commit=base)],
        repo_root=repo,
        shared_roles=set(),
        now=_committer_time(repo, tip),
    )
    assert "sonarr" in stale


def test_pending_services_are_named_on_an_up_and_do_not_move_the_exit_code():
    text, code = pr.format_stale_only(
        {}, missing=[], pending={"code-server": 720}, grace_seconds=3600
    )
    assert code == 0
    assert "code-server (12 min ago)" in text
    assert "60-min grace" in text
    kuma_text, kuma_code = pr.format_stale_kuma(
        {}, missing=[], pending={"code-server": 720}, grace_seconds=3600
    )
    assert kuma_code == 0
    assert "code-server (12 min ago)" in kuma_text
