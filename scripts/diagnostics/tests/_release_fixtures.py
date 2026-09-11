"""Fakes and git-repo fixtures shared by the two `probe.py releases` suites.

`test_probe_releases.py` (the reader: flags, lookup, loading, missing_services) and
`test_probe_releases_stale.py` (staleness over real git history) both build a release record,
and the second needs throwaway repos. They live here rather than in a conftest because
`scripts/conftest.py` covers every suite under `scripts/`, while these names are wanted by
exactly two modules -- the same split `_probe_health_fixtures.py` makes beside them.

Import by bare name: pytest puts a test's own directory on `sys.path`, so
`from _release_fixtures import _record` resolves from any module in this directory.
"""

import os
import subprocess

# git subprocesses in the fixtures below must see ONLY the tmp repo they're pointed at via
# `cwd`. `GIT_DIR`/`GIT_WORK_TREE`/`GIT_INDEX_FILE` and friends override that from the
# environment, and prek's own `pytest` hook runs THIS suite from inside a `git commit` --
# with those exact vars set to ITS OWN in-progress commit. Without stripping them, `_run_git`
# was committing into the outer repo's half-built index instead of the fixture, and prek's
# commit failed on a nested "commit -q -m 'sonarr v1'" it never asked for.
_GIT_CLEAN_ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _record(
    service="sonarr", commit="a" * 40, dirty=False, applied="2026-08-29T20:00:00Z"
):
    return {
        "service": service,
        "commit": commit,
        "commit_short": commit[:8],
        "tree_dirty": dirty,
        "applied_at": applied,
        "render_dir": f"/etc/rancher/k3s/manifests/{service}",
        "manifests": {"deployment.yaml": "sha256:1", "service.yaml": "sha256:2"},
        "manifests_digest": "deadbeef",
        "secret_manifests": [],
    }


def _run_git(repo, *args):
    subprocess.run(
        ["git", *args],
        cwd=repo,
        env=_GIT_CLEAN_ENV,
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(repo):
    repo.mkdir(parents=True, exist_ok=True)
    _run_git(repo, "init", "-q", "-b", "master")
    _run_git(repo, "config", "user.email", "test@example.com")
    _run_git(repo, "config", "user.name", "Test")
    _run_git(repo, "config", "commit.gpgsign", "false")


def _commit(repo, files, message):
    """Write `files` ({relative path: content}), commit, and return the new SHA."""
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-q", "-m", message)
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        env=_GIT_CLEAN_ENV,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _set_origin_master(repo, sha):
    _run_git(repo, "update-ref", "refs/remotes/origin/master", sha)
