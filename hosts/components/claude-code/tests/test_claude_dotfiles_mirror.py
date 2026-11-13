"""claude_dotfiles_mirror.run against a real origin repo and real SSH-signed tags in tmp_path.

The signature check is the module's whole job, so these sign with ssh-keygen keys rather than
stub git: an allowed key, a second key the signers file does not name, and a lightweight tag.
"""

import json
import os
import subprocess

import claude_dotfiles_mirror as mirror
import pytest

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "PATH": os.environ["PATH"],
}


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        env=GIT_ENV,
        check=True,
        timeout=60,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _key(tmp_path, name):
    path = tmp_path / name
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)],
        check=True,
        timeout=60,
    )
    return path


@pytest.fixture
def world(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    allowed, other = _key(tmp_path, "allowed"), _key(tmp_path, "other")
    signers = tmp_path / "allowed_signers"
    public = (tmp_path / "allowed.pub").read_text().strip()
    signers.write_text(f'operator namespaces="git" {public}\n')
    config = {"repo": str(origin), "allowed_signers": str(signers)}
    return origin, allowed, other, config, tmp_path / "state"


def _commit(origin, message):
    _git(origin, "commit", "-q", "--allow-empty", "-m", message)
    return _git(origin, "rev-parse", "HEAD")


def _tag(origin, name, key=None, at="HEAD"):
    if key is None:
        _git(origin, "tag", name, at)
        return
    _git(
        origin, "-c", "gpg.format=ssh", "-c", f"user.signingkey={key}",
        "tag", "-s", "-m", name, name, at,
    )  # fmt: skip


def _record(state):
    return json.loads((state / "verified.json").read_text())


def test_a_repo_with_no_tag_is_up_and_records_nothing(world):
    origin, _, _, config, state = world
    _commit(origin, "one")
    assert mirror.run(config, state) == (True, "no tag yet; nothing verified")
    assert not (state / "verified.json").exists()


def test_a_tag_the_operator_signed_is_recorded(world):
    origin, allowed, _, config, state = world
    commit = _commit(origin, "one")
    _tag(origin, "t2-1", allowed)
    ok, line = mirror.run(config, state)
    assert ok, line
    assert _record(state)["tag"] == "t2-1" and _record(state)["commit"] == commit


@pytest.mark.parametrize("signer", ["other", None], ids=["another key", "lightweight"])
def test_a_tag_without_the_operators_signature_is_refused(world, signer):
    origin, _, other, config, state = world
    _commit(origin, "one")
    _tag(origin, "t2-1", other if signer else None)
    ok, _ = mirror.run(config, state)
    assert not ok
    assert not (state / "verified.json").exists()


def test_a_newer_tag_on_an_older_commit_is_refused_and_the_last_is_kept(world):
    origin, allowed, _, config, state = world
    first = _commit(origin, "one")
    second = _commit(origin, "two")
    _tag(origin, "t2-2", allowed)
    assert mirror.run(config, state)[0]
    _git(origin, "checkout", "-q", "-b", "side", first)
    _commit(origin, "side")
    _tag(origin, "t2-3", allowed)
    ok, line = mirror.run(config, state)
    assert not ok and "does not descend" in line
    assert _record(state)["commit"] == second


def test_a_signed_descendant_tag_moves_the_record_forward(world):
    origin, allowed, _, config, state = world
    _commit(origin, "one")
    _tag(origin, "t2-1", allowed)
    assert mirror.run(config, state)[0]
    newer = _commit(origin, "two")
    _tag(origin, "t2-2", allowed)
    assert mirror.run(config, state) == (True, f"verified tag t2-2 at {newer[:12]}")
    assert _record(state)["commit"] == newer
