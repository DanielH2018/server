"""Tests for `host_secrets.py`, the per-host SOPS files' check and operator-only sync.

`check` is driven over real throwaway git repositories, because `git merge-base
--is-ancestor` and `git rev-parse --is-shallow-repository` are git's own semantics and a fake
history would only restate the implementation. `sync` runs against a stub `sops` runner and
a fixture tree; nothing here decrypts a real file or holds a real key.

Run: uv run pytest scripts/secrets_mgmt/tests/test_host_secrets.py
"""

import json
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from _rotation_fakes import Fakes, build_tools, process_calls
from lib import yaml_fast
from lib.git_testing import commit, git, init_repo
from lib.repo_paths import REPO
from secrets_mgmt import host_secrets as hs
from secrets_mgmt import secret_rotation as sr
from secrets_mgmt.git_dates import (
    ciphertext_rotation_commits,
    ciphertext_rotation_dates,
)
from secrets_mgmt.rotation_tools import RotationTools

HOST = "daniel-test"
HOST_FILE = f"ansible/vars/hosts/{HOST}.sops.yaml"
SECRETS = "ansible/vars/secrets.yml"
PACKAGE = f"hosts/{HOST}/package.yml"

# The live host files. H4.2 added daniel-server's and H4.4 adds the Pi's; the equality below then
# fails until the new member is named, so the live check can never pass over an empty set.
LIVE_HOST_FILES: frozenset[str] = frozenset({"daniel-server"})


def enc(tag: str) -> str:
    """A value shaped like a SOPS string. The tag stands in for the ciphertext's bytes."""
    return f"ENC[AES256_GCM,data:{tag},iv:x,tag:y,type:str]"


def sops_file(**keys: str) -> str:
    return yaml.safe_dump(
        {**{k: enc(v) for k, v in keys.items()}, "sops": {"mac": "m"}}
    )


def package(*keys: str) -> str:
    return yaml.safe_dump({"description": "fixture", "credentials": list(keys)})


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return init_repo(tmp_path / "repo", initial_commit="init")


def tools_for(path: Path) -> RotationTools:
    return RotationTools(git=lambda *args: git(path, *args).stdout)


def seed(repo: Path, keys: tuple[str, ...] = ("tok_a", "tok_b")) -> None:
    """Commit secrets.yml, the host's package and a host file synced after both."""
    commit(repo, "secrets", **{SECRETS: sops_file(**{k: "v1" for k in keys})})
    commit(repo, "tree", **{PACKAGE: package(*keys)})
    commit(repo, "sync", **{HOST_FILE: sops_file(**{k: "v1" for k in keys})})


def rotate(repo: Path, key: str, tag: str, keys=("tok_a", "tok_b")) -> str:
    values = {k: "v1" for k in keys} | {key: tag}
    return commit(repo, f"rotate {key}", **{SECRETS: sops_file(**values)})


# ── check: the accept / reject pairs ────────────────────────────────────────
def test_a_host_file_synced_after_secrets_is_clean(repo):
    seed(repo)
    assert hs.check(repo, tools_for(repo)) == []


def test_a_host_file_missing_a_key_its_tree_names_is_flagged(repo):
    commit(repo, "secrets", **{SECRETS: sops_file(tok_a="v1", tok_b="v1")})
    commit(repo, "tree", **{PACKAGE: package("tok_a", "tok_b")})
    commit(repo, "sync", **{HOST_FILE: sops_file(tok_a="v1")})
    problems = hs.check(repo, tools_for(repo))
    assert [p for p in problems if "tok_b" in p and "lacks it" in p]


def test_a_host_file_with_an_extra_key_is_flagged(repo):
    commit(repo, "secrets", **{SECRETS: sops_file(tok_a="v1", tok_b="v1")})
    commit(repo, "tree", **{PACKAGE: package("tok_a")})
    commit(repo, "sync", **{HOST_FILE: sops_file(tok_a="v1", tok_b="v1")})
    problems = hs.check(repo, tools_for(repo))
    assert [p for p in problems if "tok_b" in p and "is not named" in p]


def test_a_tree_that_names_keys_with_no_host_file_is_flagged(repo):
    commit(repo, "secrets", **{SECRETS: sops_file(tok_a="v1")})
    commit(repo, "tree", **{PACKAGE: package("tok_a")})
    assert [p for p in hs.check(repo, tools_for(repo)) if "is missing" in p]


def test_a_tree_that_names_no_key_needs_no_host_file(repo):
    commit(repo, "tree", **{PACKAGE: package()})
    assert hs.check(repo, tools_for(repo)) == []


def test_a_stale_copy_is_flagged_even_when_every_commit_is_on_one_day(repo):
    seed(repo)
    rotate(repo, "tok_a", "v2")
    problems = hs.check(repo, tools_for(repo))
    assert [p for p in problems if "tok_a" in p and "older than" in p]
    assert not [p for p in problems if "tok_b" in p], "an untouched key stays clean"


def test_a_rotation_and_its_sync_in_one_commit_are_clean(repo):
    seed(repo)
    commit(
        repo,
        "rotate and sync",
        **{
            SECRETS: sops_file(tok_a="v2", tok_b="v1"),
            HOST_FILE: sops_file(tok_a="v2", tok_b="v1"),
        },
    )
    assert hs.check(repo, tools_for(repo)) == []


def test_a_sync_after_the_rotation_clears_the_flag(repo):
    seed(repo)
    rotate(repo, "tok_a", "v2")
    commit(repo, "sync", **{HOST_FILE: sops_file(tok_a="v2", tok_b="v1")})
    assert hs.check(repo, tools_for(repo)) == []


def test_a_lagging_registry_date_does_not_flag_a_synced_copy(repo):
    """The check reads git, not `last_rotated`, so a registry behind git changes nothing."""
    seed(repo)
    registry = {"entries": {"tok_a": {"tier": "auto", "last_rotated": "2025-01-01"}}}
    commit(
        repo, "registry", **{"ansible/secret_rotation.yml": yaml.safe_dump(registry)}
    )
    assert hs.check(repo, tools_for(repo)) == []


def test_a_key_git_has_no_commit_for_is_flagged(repo):
    commit(repo, "secrets", **{SECRETS: sops_file(tok_a="v1")})
    commit(repo, "tree", **{PACKAGE: package("tok_a")})
    path = repo / HOST_FILE
    path.parent.mkdir(parents=True)
    path.write_text(sops_file(tok_a="v1"))  # written, never committed
    assert [p for p in hs.check(repo, tools_for(repo)) if "no commit in git" in p]


def test_a_key_secrets_yml_does_not_hold_is_flagged(repo):
    commit(repo, "secrets", **{SECRETS: sops_file(other="v1")})
    commit(repo, "tree", **{PACKAGE: package("tok_a")})
    commit(repo, "sync", **{HOST_FILE: sops_file(tok_a="v1")})
    assert [
        p for p in hs.check(repo, tools_for(repo)) if f"no commit in {SECRETS}" in p
    ]


def test_a_shallow_clone_is_flagged_and_a_full_clone_is_not(repo, tmp_path):
    seed(repo)
    full = tmp_path / "full"
    git(tmp_path, "clone", "-q", f"file://{repo}", str(full))
    assert hs.check(full, tools_for(full)) == []
    shallow = tmp_path / "shallow"
    git(tmp_path, "clone", "-q", "--depth", "1", f"file://{repo}", str(shallow))
    assert [p for p in hs.check(shallow, tools_for(shallow)) if "shallow" in p]


def test_a_value_that_is_not_ciphertext_is_flagged_without_quoting_it(repo):
    commit(repo, "secrets", **{SECRETS: sops_file(tok_a="v1")})
    commit(repo, "tree", **{PACKAGE: package("tok_a")})
    commit(repo, "sync", **{HOST_FILE: "tok_a: hunter2-plaintext\nsops: {mac: m}\n"})
    problems = hs.check(repo, tools_for(repo))
    assert [p for p in problems if "tok_a is not SOPS ciphertext" in p]
    assert "hunter2" not in "\n".join(problems)


def test_a_sync_that_rewrites_ten_or_more_keys_is_clean(repo):
    """The host-file walk runs without the re-encrypt guard, so a 12-key sync still counts."""
    keys = tuple(f"tok_{i:02d}" for i in range(12))
    commit(repo, "secrets", **{SECRETS: sops_file(**{k: "v1" for k in keys})})
    commit(repo, "tree", **{PACKAGE: package(*keys)})
    commit(repo, "sync", **{HOST_FILE: sops_file(**{k: "v1" for k in keys})})
    rotate(repo, "tok_00", "v2", keys)
    # A real sync gives every value a fresh nonce, so every ciphertext changes.
    renewed = {k: ("v2" if k == "tok_00" else "v1") + "-nonce2" for k in keys}
    resync = commit(repo, "resync", **{HOST_FILE: sops_file(**renewed)})
    assert ciphertext_rotation_commits(tools_for(repo), HOST_FILE)["tok_00"] == resync
    assert hs.check(repo, tools_for(repo)) == []


def test_only_the_secrets_walk_skips_a_whole_file_reencrypt(repo):
    keys = tuple(f"tok_{i:02d}" for i in range(12))
    first = commit(
        repo,
        "first",
        **{
            SECRETS: sops_file(**dict.fromkeys(keys, "a")),
            HOST_FILE: sops_file(**dict.fromkeys(keys, "a")),
        },
    )
    renewed = dict.fromkeys(keys, "b")
    second = commit(
        repo,
        "re-encrypt",
        **{SECRETS: sops_file(**renewed), HOST_FILE: sops_file(**renewed)},
    )
    tools = tools_for(repo)
    assert ciphertext_rotation_commits(tools, SECRETS)["tok_01"] == first
    assert ciphertext_rotation_commits(tools, HOST_FILE)["tok_01"] == second
    assert ciphertext_rotation_dates(tools, HOST_FILE).keys() == set(keys)


def test_the_live_tree_holds_exactly_the_named_host_files_and_checks_clean():
    assert frozenset(hs.host_files(REPO)) == LIVE_HOST_FILES
    assert hs.check(REPO, RotationTools()) == []


# ── tree_keys ───────────────────────────────────────────────────────────────
def test_tree_keys_reads_credentials_and_output_keys_from_package_and_components(
    tmp_path,
):
    (tmp_path / "hosts" / "h").mkdir(parents=True)
    (tmp_path / "hosts" / "components" / "c").mkdir(parents=True)
    (tmp_path / "hosts" / "h" / "package.yml").write_text(
        yaml.safe_dump({"components": ["c", "../x"], "credentials": ["a"]})
    )
    (tmp_path / "hosts" / "components" / "c" / "component.yml").write_text(
        yaml.safe_dump({"outputs": [{"template": "t", "keys": ["b", "c"]}]})
    )
    assert hs.tree_keys(tmp_path, "h") == {"a", "b", "c"}
    assert hs.tree_keys(tmp_path, "nobody") == frozenset()


# ── main ────────────────────────────────────────────────────────────────────
def test_main_check_exits_one_on_a_stale_copy_and_zero_on_a_clean_one(repo, capsys):
    seed(repo)
    assert hs.main(["check"], repo, tools_for(repo)) == 0
    rotate(repo, "tok_a", "v2")
    assert hs.main(["check"], repo, tools_for(repo)) == 1
    assert "tok_a" in capsys.readouterr().err


# ── sync, against a stub sops ───────────────────────────────────────────────
SECRET_VALUES = {"tok_a": "plain-a", "tok_b": "plain-b", "unrelated": "plain-u"}


def stub_sops(calls: list, values=SECRET_VALUES, rc: int = 0):
    """A `subprocess.run` stand-in for sops: decrypt answers `values`, encrypt wraps stdin."""

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if rc:
            raise subprocess.CalledProcessError(rc, cmd, output="plain-a leaked")
        if "decrypt" in cmd:
            return subprocess.CompletedProcess(cmd, 0, json.dumps(values), "")
        wrapped = {k: enc(k) for k in yaml_fast.safe_load(kwargs["input"])}
        return subprocess.CompletedProcess(cmd, 0, yaml.safe_dump(wrapped), "")

    return run


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    (tmp_path / "hosts" / HOST).mkdir(parents=True)
    (tmp_path / PACKAGE).write_text(package("tok_a", "tok_b"))
    return tmp_path


def test_sync_writes_only_the_keys_the_tree_names_and_keeps_values_off_argv(
    tree, capsys
):
    calls: list = []
    assert hs.sync(tree, HOST, stub_sops(calls)) == 0
    written = yaml_fast.safe_load((tree / HOST_FILE).read_text())
    assert set(written) == {"tok_a", "tok_b"}
    assert all(v.startswith("ENC[") for v in written.values())
    argv = " ".join(" ".join(cmd) for cmd, _ in calls)
    assert "plain-" not in argv
    encrypt = [kw for cmd, kw in calls if "encrypt" in cmd][0]
    assert "plain-a" in encrypt["input"], "the plaintext travels on stdin"
    assert "--filename-override" in " ".join(
        next(c for c, _ in calls if "encrypt" in c)
    )
    out = capsys.readouterr()
    assert "plain-" not in out.out + out.err


def test_sync_refuses_a_key_secrets_yml_lacks_and_writes_nothing(tree, capsys):
    values = {"tok_a": "plain-a"}
    assert hs.sync(tree, HOST, stub_sops([], values)) == 1
    assert "tok_b" in capsys.readouterr().err
    assert not (tree / HOST_FILE).exists()


def test_sync_keeps_the_old_file_when_sops_fails_and_prints_no_sops_output(
    tree, capsys
):
    path = tree / HOST_FILE
    path.parent.mkdir(parents=True)
    path.write_text("old\n")
    assert hs.sync(tree, HOST, stub_sops([], rc=1)) == 1
    assert path.read_text() == "old\n"
    assert not list(path.parent.glob("*.tmp"))
    out = capsys.readouterr()
    assert "leaked" not in out.out + out.err


def test_sync_refuses_a_host_whose_tree_names_no_key_and_a_non_plain_name(tree, capsys):
    (tree / "hosts" / "bare").mkdir()
    (tree / "hosts" / "bare" / "package.yml").write_text(package())
    assert hs.sync(tree, "bare", stub_sops([])) == 1
    assert hs.sync(tree, "../etc", stub_sops([])) == 1
    assert not (tree / "ansible").exists()


# ── the unattended rotation skips every key a host file carries ─────────────
def _rotate_args():
    return SimpleNamespace(
        name=None, all=True, commit=True, deploy=False, no_derive=False
    )


NAME = "monitor_bridge_test_token"  # a name consumer_tags() resolves, so only host_keys differs


def _fixture(**overrides) -> Fakes:
    registry = {"entries": {NAME: {"tier": "auto", "last_rotated": "2026-01-01"}}}
    return replace(Fakes(registry=registry), **overrides)


def test_unattended_rotate_skips_a_key_a_host_file_carries_and_says_so(capsys):
    tools, recorded = build_tools(_fixture(host_keys=frozenset({NAME})))
    assert sr.cmd_rotate(_rotate_args(), tools) == 0
    assert process_calls(recorded) == [], "a host-file key must never reach `sops set`"
    assert "skip (manual: a host file carries it) %s" % NAME in capsys.readouterr().out


def test_unattended_rotate_still_rotates_the_same_key_when_no_host_file_carries_it():
    tools, recorded = build_tools(_fixture())
    assert sr.cmd_rotate(_rotate_args(), tools) == 0
    assert len(process_calls(recorded)) == 1
