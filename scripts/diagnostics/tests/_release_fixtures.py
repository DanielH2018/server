"""Fakes and git-repo fixtures shared by the two `probe.py releases` suites.

`test_probe_releases.py` (the reader: flags, lookup, loading, missing_services) and
`test_probe_releases_stale.py` (staleness over real git history) both build a release record,
and the second needs throwaway repos. They live here rather than in a conftest because
`scripts/conftest.py` covers every suite under `scripts/`, while these names are wanted by
exactly two modules -- the same split `_probe_health_fixtures.py` makes beside them.

Import by bare name: pytest puts a test's own directory on `sys.path`, so
`from _release_fixtures import _record` resolves from any module in this directory.
"""

from lib.git_testing import commit, git, init_repo


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


def _init_repo(repo):
    init_repo(repo)


def _commit(repo, files, message):
    """Write `files` ({relative path: content}), commit, and return the new SHA."""
    return commit(repo, message, **files)


def _set_origin_master(repo, sha):
    git(repo, "update-ref", "refs/remotes/origin/master", sha)
