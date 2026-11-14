"""Tests for scripts/secrets_mgmt/stale_copies.py over a fixture git repo.

The fixture's `pod_token` is read by a k8s role, `box_token` by daniel-box's package, and
`sonarr_api_key` by a k8s role and by Bazarr's PVC. The ciphertexts are stand-ins: nothing
here decrypts, so only whether a string changed matters.

Run: uv run pytest scripts/secrets_mgmt/tests/test_stale_copies.py
"""

from pathlib import Path

import pytest

from lib.git_testing import commit, git, init_repo
from secrets_mgmt.cluster_vars_check import digest
from secrets_mgmt.stale_copies import unapplied_keys

_OLD, _NEW = "ENC[AES256_GCM,data:old,type:str]", "ENC[AES256_GCM,data:new,type:str]"


def _store(**values: str) -> str:
    return "".join(f"{k}: {v}\n" for k, v in values.items()) + "sops:\n  version: 3\n"


def _source(**values: str) -> str:
    return "".join(f"{k} {digest(v)}\n" for k, v in values.items())


def _rotation(tmp_path: Path, before: dict, after: dict) -> tuple[str, str, str]:
    """A repo committed at `before`, then at `after`; (repo, old sha, new sha)."""
    repo = init_repo(tmp_path / "repo")
    template = "k8s/roles/sonarr/templates/secret.yaml.j2"
    files = {
        template: "a: {{ pod_token }}\nb: {{ sonarr_api_key }}\n",
        "hosts/daniel-box/package.yml": "credentials:\n  - box_token\n",
    }
    old = commit(repo, "before", **files, **before)
    return str(repo), old, commit(repo, "after", **after)


def _keys(**values: str) -> dict:
    """The store, the cluster file's digests and daniel-box's file, all at `values`."""
    return {
        "secrets/secrets.yml": _store(**values),
        "secrets/cluster-vars.source": _source(**values),
        "secrets/hosts/daniel-box.sops.yaml": _store(box_token=values["box_token"]),
    }


def test_a_rotation_whose_every_copy_was_refreshed_leaves_nothing_stale(tmp_path):
    repo, old, new = _rotation(
        tmp_path,
        _keys(pod_token=_OLD, box_token=_OLD, sonarr_api_key=_OLD),
        _keys(pod_token=_NEW, box_token=_NEW, sonarr_api_key=_OLD),
    )
    assert unapplied_keys(old, new, repo) == {}


def test_a_stale_cluster_file_names_the_key(tmp_path):
    before = _keys(pod_token=_OLD, box_token=_OLD, sonarr_api_key=_OLD)
    after = before | {
        "secrets/secrets.yml": _store(
            pod_token=_NEW, box_token=_OLD, sonarr_api_key=_OLD
        )
    }
    repo, old, new = _rotation(tmp_path, before, after)
    assert unapplied_keys(old, new, repo) == {
        "pod_token": "the cluster file's copy is stale (`cluster_vars.py write`)"
    }


def test_an_unsynced_host_file_names_the_host(tmp_path):
    before = _keys(pod_token=_OLD, box_token=_OLD, sonarr_api_key=_OLD)
    after = _keys(pod_token=_OLD, box_token=_NEW, sonarr_api_key=_OLD) | {
        "secrets/hosts/daniel-box.sops.yaml": _store(box_token=_OLD)
    }
    repo, old, new = _rotation(tmp_path, before, after)
    assert unapplied_keys(old, new, repo) == {
        "box_token": "daniel-box's host file is not synced "
        "(`host_secrets.py sync daniel-box`)"
    }


def test_a_key_with_a_copy_outside_the_tree_is_stale_however_fresh_the_rest(tmp_path):
    repo, old, new = _rotation(
        tmp_path,
        _keys(pod_token=_OLD, box_token=_OLD, sonarr_api_key=_OLD),
        _keys(pod_token=_OLD, box_token=_OLD, sonarr_api_key=_NEW),
    )
    assert unapplied_keys(old, new, repo) == {
        "sonarr_api_key": "Bazarr's config on the bazarr-config PVC holds a copy no "
        "deploy reaches"
    }


def test_a_key_no_file_reads_is_stale(tmp_path):
    keys = {"pod_token": _OLD, "box_token": _OLD, "sonarr_api_key": _OLD}
    repo, old, new = _rotation(
        tmp_path, _keys(**keys), _keys(**keys, ui_login_password=_NEW)
    )
    assert list(unapplied_keys(old, new, repo)) == ["ui_login_password"]


@pytest.mark.parametrize(
    "reader, why",
    [
        (
            {"k8s/roles/late/templates/secret.yaml.j2": "t: {{ late_token }}\n"},
            "the cluster file's copy is stale (`cluster_vars.py write`)",
        ),
        (
            {"hosts/daniel-server/package.yml": "credentials:\n  - late_token\n"},
            "daniel-server's host file is not synced "
            "(`host_secrets.py sync daniel-server`)",
        ),
    ],
    ids=["k8s", "host"],
)
def test_a_reader_the_rotation_added_counts_with_the_working_tree_at_old(
    tmp_path, reader, why
):
    keys = {"pod_token": _OLD, "box_token": _OLD, "sonarr_api_key": _OLD}
    before = _keys(**keys, late_token=_OLD)
    after = before | {"secrets/secrets.yml": _store(**keys, late_token=_NEW), **reader}
    repo, old, new = _rotation(tmp_path, before, after)
    git(repo, "checkout", "-q", old)
    assert unapplied_keys(old, new, repo) == {"late_token": why}


def test_a_reader_only_the_merge_commit_holds_counts(tmp_path):
    keys = {"pod_token": _OLD, "box_token": _OLD, "sonarr_api_key": _OLD}
    before = _keys(**keys, late_token=_OLD)
    after = before | {"secrets/secrets.yml": _store(**keys, late_token=_NEW)}
    repo, old, head = _rotation(tmp_path, before, after)
    merged = commit(
        Path(repo),
        "merge with a PR that added a reader",
        **{"k8s/roles/late/templates/secret.yaml.j2": "t: {{ late_token }}\n"},
    )
    assert unapplied_keys(old, head, repo, deployed=merged) == {
        "late_token": "the cluster file's copy is stale (`cluster_vars.py write`)"
    }
