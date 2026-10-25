"""The k3s binary-sum co-change check (H6.3e): a version bump must carry a new sum.

Run: uv run pytest scripts/validate/tests/test_validate_k3s_binary_sum.py
"""

from pathlib import Path

import pytest
from lib import proc_testing, yaml_fast
from lib.repo_paths import SYSTEM_UPGRADE_DEFAULTS

from validate import k3s_binary_sum as sut

PATH = "defaults.yml"
SUM_A, SUM_B = "a" * 64, "b" * 64


def _doc(version: str, digest: str) -> str:
    return f"k3s_version: {version}\nk3s_binary_sha256_amd64: {digest}\n"


def _git(repo: Path, *args: str) -> str:
    env = {"GIT_CONFIG_GLOBAL": "/dev/null", "HOME": str(repo), "PATH": "/usr/bin:/bin"}
    return proc_testing.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=repo,
        env=env,
        check=True,
    ).stdout.strip()


def _commit(repo: Path, text: str) -> str:
    (repo / PATH).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--no-gpg-sign", "-m", "step")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "master")
    return tmp_path


def test_a_bump_that_moves_the_sum_is_clean(repo, capsys):
    base = _commit(repo, _doc("v1.37.1+k3s1", SUM_A))
    head = _commit(repo, _doc("v1.37.2+k3s1", SUM_B))
    assert sut.check(base, head, repo, PATH) == sut.CLEAN
    assert "keeps the version and sum together" in capsys.readouterr().out


def test_a_bump_that_leaves_the_sum_is_refused(repo, capsys):
    base = _commit(repo, _doc("v1.37.1+k3s1", SUM_A))
    head = _commit(repo, _doc("v1.37.2+k3s1", SUM_A))
    assert sut.check(base, head, repo, PATH) == sut.REFUSED
    assert "k3s_binary_sha256_amd64 did not" in capsys.readouterr().err
    problems = sut.problems(
        sut.keys_at(base, PATH, repo), sut.keys_at(head, PATH, repo)
    )
    assert problems


def test_a_malformed_sum_is_refused(repo):
    base = _commit(repo, _doc("v1.37.1+k3s1", SUM_A))
    head = _commit(repo, _doc("v1.37.1+k3s1", "abc123"))
    assert sut.check(base, head, repo, PATH) == sut.REFUSED


def test_the_pr_that_introduces_the_sum_is_clean(repo):
    base = _commit(repo, "k3s_version: v1.37.1+k3s1\n")
    head = _commit(repo, _doc("v1.37.1+k3s1", SUM_A))
    assert sut.check(base, head, repo, PATH) == sut.CLEAN


def test_a_missing_key_is_unjudged(repo):
    base = _commit(repo, _doc("v1.37.1+k3s1", SUM_A))
    head = _commit(repo, "k3s_version: v1.37.2+k3s1\n")
    assert sut.check(base, head, repo, PATH) == sut.UNJUDGED


def test_the_real_defaults_hold_both_keys_in_shape():
    """Names the real file as the member the check must find."""
    data = yaml_fast.safe_load(SYSTEM_UPGRADE_DEFAULTS.read_text())
    keys = {k: data[k] for k in (sut.VERSION_KEY, sut.SUM_KEY)}
    assert sut.problems(keys, keys) == []
