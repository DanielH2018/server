#!/usr/bin/env python3
"""Tests for the Grafana dashboard validator.

The duplicate-uid rule is the one with a live incident behind it. Grafana does not resolve a
duplicate uid by picking a winner — its file provisioner disables database writes for the whole
provider, so ONE duplicated pair freezes every dashboard in the estate at its last-written
version while the pod stays 1/1 Running and its health probe stays green.

Run: uv run pytest scripts/validate/tests/test_validate_grafana_dashboards.py
"""

import json

from validate import grafana_dashboards as vgd


# ── duplicate dashboard uids ───────────────────────────────────────────────────────────────────


def test_a_uid_claimed_by_two_files_is_flagged():
    """The rejecting half — the exact shape of the 2026-08-28 incident, where running the
    exporter wrote slug-named copies beside hand-named originals."""
    errors = vgd.duplicate_dashboard_uids(
        {
            "longhorn-storage": [
                "Infrastructure/longhorn.json",
                "Infrastructure/longhorn-storage.json",
            ],
            "cadvisor": ["Infrastructure/cadvisor.json"],
        }
    )
    assert len(errors) == 1
    assert "longhorn-storage" in errors[0]
    assert "longhorn.json" in errors[0] and "longhorn-storage.json" in errors[0]


def test_one_file_per_uid_is_clean():
    """The accepting half. A rule that flagged every uid would pass the test above too."""
    assert (
        vgd.duplicate_dashboard_uids(
            {"a": ["AI/a.json"], "b": ["Apps/b.json"], "c": ["Logs/c.json"]}
        )
        == []
    )


def test_every_duplicated_uid_is_reported_not_just_the_first():
    """Eight pairs existed at once; reporting only the first would mean eight fix-and-rerun
    cycles to clear them."""
    errors = vgd.duplicate_dashboard_uids(
        {"a": ["x.json", "y.json"], "b": ["p.json", "q.json"], "c": ["ok.json"]}
    )
    assert len(errors) == 2


def test_the_real_tree_has_no_duplicate_uids():
    """The regression guard over the real dashboards.

    19 boards, all uids distinct when the eight legacy copies were deleted.
    """
    assert [e for e in vgd.validate() if "claimed by" in e] == []


# ── the rule reads the real files, not just a hand-built dict ─────────────────────────────────


def test_validate_catches_a_duplicate_written_to_disk(tmp_path):
    """Binds duplicate_dashboard_uids() into validate()'s file walk.

    Testing only the pure helper would pass even if validate() never collected the uids — the
    inert-check shape.
    """
    board = {
        "uid": "shared-uid",
        "title": "Board",
        "panels": [],
        "annotations": {"list": []},
    }
    (tmp_path / "one.json").write_text(json.dumps(board))
    (tmp_path / "two.json").write_text(json.dumps(board))

    errors = vgd.validate(dashboards_dir=tmp_path)
    assert any("shared-uid" in e and "claimed by 2 files" in e for e in errors)


def test_validate_passes_two_boards_with_distinct_uids(tmp_path):
    """The accepting mirror of the file-walk test."""
    for name, uid in (("one.json", "uid-a"), ("two.json", "uid-b")):
        (tmp_path / name).write_text(
            json.dumps(
                {"uid": uid, "title": name, "panels": [], "annotations": {"list": []}}
            )
        )
    assert [e for e in vgd.validate(dashboards_dir=tmp_path) if "claimed by" in e] == []
