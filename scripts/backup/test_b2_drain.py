#!/usr/bin/env python3
"""Guards on the B2 prefix drain.

This tool deletes objects from the only offsite copy of every Longhorn volume, so the parts
worth testing are the ones that decide WHAT gets deleted and the one that decides whether a
deletion actually happened.

Run: uv run pytest scripts/backup/test_b2_drain.py
"""

import pytest

from b2_drain import (
    BACKUPSTORE_PREFIX,
    DrainError,
    classify,
    current_versions,
    parse_volume_list,
    live_object_count,
    volume_of,
    volume_prefix,
)

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
    """Reading `action == upload` across all versions made a finished deletion look like a
    no-op on 2026-08-19, and produced a false 'Longhorn strands objects' finding."""
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
