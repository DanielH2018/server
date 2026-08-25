"""build_docs must degrade rather than abort.

One failing generator leaves one stale page. One failing generator that aborts the
run leaves every page stale, and the site build never happens at all.

Run: uv run pytest scripts/docs/test_build_docs.py
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import json

import build_docs


def test_a_failing_generator_does_not_stop_the_others(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        code = 1 if "service_catalog.py" in " ".join(argv) else 0
        return subprocess.CompletedProcess(argv, code, "", "boom")

    monkeypatch.setattr(build_docs.subprocess, "run", fake_run)
    build_docs.run_generators()

    ran = " ".join(" ".join(c) for c in calls)
    assert "gen_infra_map.py" in ran, (
        "a later generator was skipped after an earlier failure"
    )


def test_run_generators_reports_which_failed(monkeypatch):
    def fake_run(argv, **kwargs):
        code = 1 if "service_catalog.py" in " ".join(argv) else 0
        return subprocess.CompletedProcess(argv, code, "", "boom")

    monkeypatch.setattr(build_docs.subprocess, "run", fake_run)
    failed = build_docs.run_generators()
    assert len(failed) == 1
    assert "service_catalog.py" in failed[0]


def test_main_exits_nonzero_when_a_generator_failed(monkeypatch):
    monkeypatch.setattr(
        build_docs, "run_generators", lambda: ["scripts/docs/service_catalog.py"]
    )
    monkeypatch.setattr(
        build_docs, "build_site", lambda site_dir, generators="ok": True
    )
    assert build_docs.main(["--site-dir", "/tmp/x"]) == 2


def test_main_builds_the_site_even_when_a_generator_failed(monkeypatch):
    """The stale-page-beats-no-page rule, asserted."""
    built: list[str] = []
    monkeypatch.setattr(
        build_docs, "run_generators", lambda: ["scripts/docs/service_catalog.py"]
    )
    monkeypatch.setattr(
        build_docs,
        "build_site",
        lambda site_dir, generators="ok": built.append(site_dir) or True,
    )
    build_docs.main(["--site-dir", "/tmp/x"])
    assert built == ["/tmp/x"]


def test_main_exits_nonzero_when_the_site_build_failed(monkeypatch):
    monkeypatch.setattr(build_docs, "run_generators", lambda: [])
    monkeypatch.setattr(
        build_docs, "build_site", lambda site_dir, generators="ok": False
    )
    assert build_docs.main(["--site-dir", "/tmp/x"]) == 3


def test_skip_generators_runs_none(monkeypatch):
    """The dirty-tree path in docs-refresh.sh depends on this.

    A dirty working tree must rebuild the site without regenerating anything, or the
    cron would mix its own writes into someone else's in-progress edit.
    """
    ran: list[str] = []
    monkeypatch.setattr(build_docs, "run_generators", lambda: ran.append("x") or [])
    monkeypatch.setattr(
        build_docs, "build_site", lambda site_dir, generators="ok": True
    )
    build_docs.main(["--site-dir", "/tmp/x", "--skip-generators"])
    assert ran == []


def test_every_generator_output_lands_under_docs():
    """A generator writing outside docs/ would escape the hand-edit hook."""
    for _argv, out in build_docs.GENERATORS:
        assert out.startswith("docs/"), f"{out} is outside docs/"


def test_a_failed_build_leaves_the_previous_site_in_place(tmp_path, monkeypatch):
    """The pod serves this directory. A failed build must not empty it.

    mkdocs cleans its --site-dir before writing, so building straight into the served
    path would blank the site on every failure and for several seconds on every
    success.
    """
    final = tmp_path / "site"
    final.mkdir()
    (final / "index.html").write_text("previous")

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, "", "build error")

    monkeypatch.setattr(build_docs.subprocess, "run", fake_run)
    assert build_docs.build_site(str(final)) is False
    assert (final / "index.html").read_text() == "previous"


def test_the_served_directory_keeps_its_inode(tmp_path, monkeypatch):
    """site_dir is a hostPath mount, and a bind mount follows the INODE, not the path.

    Replacing the directory leaves the pod mounted on the old inode, which the cleanup
    then deletes -- nginx serves an empty tree and answers 403 until someone restarts
    the pod. That happened on 2026-08-24; this is the regression guard.
    """
    final = tmp_path / "site"
    final.mkdir()
    (final / "stale.html").write_text("old")
    inode_before = final.stat().st_ino

    def fake_run(argv, **kwargs):
        # Stand in for mkdocs: populate the staging dir it was told to write to.
        if "mkdocs" in argv:
            staging = Path(argv[argv.index("--site-dir") + 1])
            staging.mkdir(parents=True, exist_ok=True)
            (staging / "index.html").write_text("new")
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(build_docs.subprocess, "run", fake_run)
    assert build_docs.build_site(str(final)) is True
    assert final.stat().st_ino == inode_before, (
        "served directory was replaced, not updated"
    )


def test_the_build_stamp_is_written_into_the_site_not_the_repo(tmp_path):
    """The cron-liveness signal must never be committed.

    Committing it would rewrite a tracked file on every run, which is exactly the
    commit-per-run problem write_if_body_changed exists to prevent.
    """
    site = tmp_path / "site"
    site.mkdir()
    build_docs._write_build_stamp(site)
    assert (site / "build-info.json").is_file()
    assert not (build_docs.REPO / "build-info.json").exists()


def test_the_stamp_says_when_the_generators_did_not_run(tmp_path):
    """build-info.json IS the freshness signal -- docs-refresh.sh runs no deadman because
    this file is the deadman. But its dirty-tree and open-PR paths rebuild the site with
    --skip-generators, and the stamp refreshed anyway: it read fresh while the pages behind
    it had not been regenerated at all, and a stuck-open PR made that self-perpetuating
    (2026-08-25 review M-4).
    """
    site = tmp_path / "site"
    site.mkdir()
    build_docs._write_build_stamp(site, "skipped")
    info = json.loads((site / "build-info.json").read_text())
    assert info["generators"] == "skipped", (
        "a site rebuilt without regeneration claims the same freshness as a full run"
    )


def test_both_skipping_call_sites_are_reported_as_skipped(monkeypatch):
    """--skip-generators is the flag BOTH deferring paths in docs-refresh.sh pass, so the
    status has to come off the flag rather than off the failure list -- which is empty in
    exactly the case that needs reporting."""
    seen = {}

    def fake_build(site_dir, generators="ok"):
        seen["generators"] = generators
        return True

    monkeypatch.setattr(build_docs, "build_site", fake_build)
    build_docs.main(["--site-dir", "/tmp/x", "--skip-generators"])
    assert seen["generators"] == "skipped"
