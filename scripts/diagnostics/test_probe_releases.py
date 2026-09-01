"""Tests for `probe.py releases` -- the reader over the k8s release records.

Every rule here is a `..._is_clean` / `..._is_flagged` pair. A check observed only from the
passing side is indistinguishable from one that fires on nothing, and this repo has paid for
that twice (volume-claim's short-circuit, image-smoke's bare-boot rule).

Run: uv run pytest scripts/diagnostics/test_probe_releases.py
"""

import json
import re
from pathlib import Path

import probe_releases as pr

REPO = Path(__file__).resolve().parents[2]


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


# ── the reader agrees with the writer about where records live ───────────────────────────────


def test_release_dir_matches_the_ansible_default():
    """A drifted path makes this reader report 'no records' on a healthy host -- silence, not an
    error, which is the failure mode a comment cannot prevent."""
    defaults = (REPO / "ansible/roles/k8s/manifests/defaults/main.yml").read_text()
    m = re.search(r"^manifests_release_dir:\s*(\S+)\s*$", defaults, re.M)
    assert m, "manifests_release_dir is not defined in the manifests role defaults"
    assert m.group(1) == str(pr.RELEASE_DIR)


# ── flags ────────────────────────────────────────────────────────────────────────────────────


def _row(text, service):
    """The service's own line. The footer legend names both flags, so asserting against the
    whole rendering would score every clean table as flagged."""
    return next(line for line in text.splitlines() if line.startswith(service))


def test_clean_merged_record_is_clean():
    text, code = pr.format_records([_record()], merged={"a" * 40})
    assert code == 0
    assert _row(text, "sonarr").endswith("-")


def test_dirty_record_is_flagged():
    text, code = pr.format_records([_record(dirty=True)], merged={"a" * 40})
    assert code == 1
    assert "dirty" in _row(text, "sonarr")


def test_unmerged_record_is_flagged():
    text, code = pr.format_records([_record()], merged=set())
    assert code == 1
    assert "unmerged" in _row(text, "sonarr")


def test_unparseable_record_is_flagged():
    text, code = pr.format_records(
        [{"service": "sonarr", "error": "boom"}], merged=set()
    )
    assert code == 1
    assert "UNREADABLE" in text


def test_no_records_is_its_own_exit_code():
    """Distinct from 'records exist and are clean' -- an empty directory means the stamp has
    never run, which is a different thing to report than a green fleet."""
    text, code = pr.format_records([], merged=set())
    assert code == 2
    assert "no release records" in text


# ── per-service lookup ───────────────────────────────────────────────────────────────────────


def test_named_service_returns_its_full_record():
    text, code = pr.format_records(
        [_record("sonarr"), _record("radarr")], merged={"a" * 40}, service="radarr"
    )
    assert code == 0
    assert json.loads(text)["service"] == "radarr"


def test_unknown_service_is_flagged():
    _, code = pr.format_records([_record("sonarr")], merged={"a" * 40}, service="nope")
    assert code == 2


# ── loading ──────────────────────────────────────────────────────────────────────────────────


def test_load_records_reads_and_orders_newest_first(tmp_path):
    (tmp_path / "sonarr.json").write_text(
        json.dumps(_record("sonarr", applied="2026-01-01T00:00:00Z"))
    )
    (tmp_path / "radarr.json").write_text(
        json.dumps(_record("radarr", applied="2026-06-01T00:00:00Z"))
    )
    got = pr.load_records(tmp_path)
    assert [r["service"] for r in got] == ["radarr", "sonarr"]


def test_load_records_skips_the_previous_files(tmp_path):
    """`*.previous.json` also matches `*.json`; without the guard every service would appear
    twice, once with a stale commit, and the table would read as drift that is not there."""
    (tmp_path / "sonarr.json").write_text(json.dumps(_record("sonarr")))
    (tmp_path / "sonarr.previous.json").write_text(json.dumps(_record("sonarr")))
    assert [r["service"] for r in pr.load_records(tmp_path)] == ["sonarr"]


def test_load_records_reports_a_truncated_record(tmp_path):
    """A half-written record must surface, not vanish -- a truncated write is exactly the case
    where a silently shorter table is worst."""
    (tmp_path / "sonarr.json").write_text('{"service": "sonarr"')
    got = pr.load_records(tmp_path)
    assert len(got) == 1 and "error" in got[0]


def test_missing_directory_is_empty_not_an_exception(tmp_path):
    assert pr.load_records(tmp_path / "absent") == []
