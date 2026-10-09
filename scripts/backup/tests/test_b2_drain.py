#!/usr/bin/env python3
"""Guards on the B2 prefix drain.

This tool deletes objects from the only offsite copy of every Longhorn volume, so the parts
worth testing are the ones that decide WHAT gets deleted and the one that decides whether a
deletion actually happened.

Run: uv run pytest scripts/backup/tests/test_b2_drain.py
"""

import pytest

from b2_drain import (
    BACKUPSTORE_PREFIX,
    DrainError,
    classify,
    current_versions,
    parse_volume_list,
    live_object_count,
    main,
    volume_of,
    volume_prefix,
)
from lib.b2 import B2Error, B2Session

VOL = "pvc-36a38101-4df3-460c-bbef-94fe8185dde9"
KEY = f"{BACKUPSTORE_PREFIX}a1/b2/{VOL}/blocks/aa/bb/deadbeef.blk"


def _v(name, action, file_id="f1"):
    return {"fileName": name, "action": action, "fileId": file_id}


def test_the_volume_is_the_third_segment_under_the_prefix():
    assert volume_of(KEY) == VOL
    assert volume_of(f"{BACKUPSTORE_PREFIX}a1/b2/{VOL}/volume.cfg") == VOL


def test_keys_outside_the_backupstore_belong_to_no_volume():
    """Attributing a stray key by guess is how a drain reaches outside its prefix."""
    assert volume_of("some/other/path/file.blk") is None
    assert volume_of(f"{BACKUPSTORE_PREFIX}a1/b2") is None


def test_the_volume_prefix_covers_exactly_one_volume():
    assert volume_prefix(KEY) == f"{BACKUPSTORE_PREFIX}a1/b2/{VOL}/"


def test_the_current_version_is_the_first_one_returned():
    """B2 returns a name's versions newest-first; the rest are retained history."""
    versions = [
        _v("a", "hide", "new"),
        _v("a", "upload", "old"),
        _v("b", "upload", "only"),
    ]
    current = current_versions(versions)
    assert current["a"]["fileId"] == "new"
    assert current["b"]["fileId"] == "only"


def test_a_hidden_file_is_not_counted_as_live():
    """Reading `action == upload` across all versions would make a finished deletion look like a
    no-op."""
    versions = [
        _v("deleted", "hide", "h"),
        _v("deleted", "upload", "u"),
        _v("kept", "upload", "k"),
    ]
    assert live_object_count(versions) == 1


def test_an_empty_live_volume_list_refuses_everything():
    """An unreadable or empty list makes every prefix look stranded — fail closed instead."""
    with pytest.raises(DrainError, match="empty"):
        classify([VOL], present={VOL}, live=set())


def test_a_volume_that_still_exists_is_refused():
    drainable, refused = classify([VOL], present={VOL}, live={VOL, "pvc-other"})
    assert drainable == []
    assert "still exists" in refused[VOL]


def test_a_volume_with_no_prefix_is_refused_rather_than_silently_skipped():
    drainable, refused = classify([VOL], present=set(), live={"pvc-other"})
    assert drainable == []
    assert "no such prefix" in refused[VOL]


def test_a_stranded_volume_is_drainable():
    drainable, refused = classify([VOL], present={VOL}, live={"pvc-other"})
    assert drainable == [VOL]
    assert refused == {}


def test_a_volume_list_parses_from_either_separator():
    """The file form exists because 20 names is 820 characters and shells wrap it."""
    assert parse_volume_list("a,b") == ["a", "b"]
    assert parse_volume_list("a\nb\n") == ["a", "b"]
    assert parse_volume_list(" a , b \n") == ["a", "b"]


def test_a_duplicated_name_is_only_drained_once():
    assert parse_volume_list("a,b,a") == ["a", "b"]


class FakeBucket:
    """A B2 bucket behind `B2Session`'s transport seam: authorize, list versions, delete.

    It refuses `b2_list_file_names`, which skips hidden versions, so a drain that stopped
    listing versions would leave them behind and still verify empty.
    """

    def __init__(self, versions, *, auth=None, fail_on=None):
        self.versions = list(versions)
        self.auth = auth or {
            "apiInfo": {
                "storageApi": {
                    "apiUrl": "https://api",
                    "bucketId": "bid",
                    "capabilities": ["listFiles", "deleteFiles"],
                }
            },
            "authorizationToken": "t",
        }
        self.fail_on = fail_on
        self.deleted = []

    def __call__(self, url, headers, payload, timeout):
        api = url.rsplit("/", 1)[-1]
        if api == self.fail_on:
            raise B2Error(
                "HTTP 403: transaction_cap_exceeded", "transaction_cap_exceeded"
            )
        if api == "b2_authorize_account":
            return self.auth
        if api == "b2_list_file_versions":
            return {
                "files": [
                    v
                    for v in self.versions
                    if v["fileName"].startswith(payload["prefix"])
                ]
            }
        if api == "b2_delete_file_version":
            self.deleted.append(payload["fileId"])
            self.versions = [
                v for v in self.versions if v["fileId"] != payload["fileId"]
            ]
            return {}
        raise AssertionError(f"the drain called {api}")


@pytest.fixture
def run(monkeypatch, tmp_path):
    """Run main() against a FakeBucket with one live volume, returning its exit code."""
    monkeypatch.setenv("B2_KEY_ID", "k")
    monkeypatch.setenv("B2_APP_KEY", "s")
    live = tmp_path / "live.txt"
    live.write_text("pvc-other\n")

    def _run(bucket, *extra):
        return main(
            ["--live-volumes-file", str(live), "--volumes", VOL, *extra],
            open_session=lambda key_id, app_key: B2Session(
                key_id, app_key, transport=bucket
            ),
        )

    return _run


def test_apply_deletes_every_version_including_hidden_ones(run, capsys):
    bucket = FakeBucket(
        [
            _v(KEY, "hide", "h1"),
            _v(KEY, "upload", "u1"),
            _v(f"{BACKUPSTORE_PREFIX}c3/d4/pvc-other/volume.cfg", "upload", "keep"),
        ]
    )
    assert run(bucket, "--apply") == 0
    assert sorted(bucket.deleted) == ["h1", "u1"]
    assert [v["fileId"] for v in bucket.versions] == ["keep"]
    assert f"verified empty: {VOL}" in capsys.readouterr().out


def test_a_dry_run_deletes_nothing(run):
    bucket = FakeBucket([_v(KEY, "upload", "u1")])
    assert run(bucket) == 0
    assert bucket.deleted == []


def test_a_key_that_is_not_bucket_scoped_is_refused_by_the_drain(run, capsys):
    bucket = FakeBucket([], auth={"apiUrl": "https://api", "authorizationToken": "t"})
    assert run(bucket, "--apply") == 2
    assert "not bucket-scoped" in capsys.readouterr().err


def test_a_b2_error_exits_2_with_its_message_rather_than_a_traceback(run, capsys):
    bucket = FakeBucket([_v(KEY, "upload", "u1")], fail_on="b2_list_file_versions")
    assert run(bucket, "--apply") == 2
    assert "transaction_cap_exceeded" in capsys.readouterr().err
    assert bucket.deleted == []
