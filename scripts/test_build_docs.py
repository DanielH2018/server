"""build_docs must degrade rather than abort.

One failing generator leaves one stale page. One failing generator that aborts the
run leaves every page stale, and the site build never happens at all.

Run: uv run pytest scripts/test_build_docs.py
"""

from __future__ import annotations

import subprocess
from pathlib import Path

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
        build_docs, "run_generators", lambda: ["scripts/service_catalog.py"]
    )
    monkeypatch.setattr(build_docs, "build_site", lambda site_dir: True)
    assert build_docs.main(["--site-dir", "/tmp/x"]) == 1


def test_main_builds_the_site_even_when_a_generator_failed(monkeypatch):
    """The stale-page-beats-no-page rule, asserted."""
    built: list[str] = []
    monkeypatch.setattr(
        build_docs, "run_generators", lambda: ["scripts/service_catalog.py"]
    )
    monkeypatch.setattr(
        build_docs, "build_site", lambda site_dir: built.append(site_dir) or True
    )
    build_docs.main(["--site-dir", "/tmp/x"])
    assert built == ["/tmp/x"]


def test_main_exits_nonzero_when_the_site_build_failed(monkeypatch):
    monkeypatch.setattr(build_docs, "run_generators", lambda: [])
    monkeypatch.setattr(build_docs, "build_site", lambda site_dir: False)
    assert build_docs.main(["--site-dir", "/tmp/x"]) == 1


def test_skip_generators_runs_none(monkeypatch):
    """The dirty-tree path in docs-refresh.sh depends on this.

    A dirty working tree must rebuild the site without regenerating anything, or the
    cron would mix its own writes into someone else's in-progress edit.
    """
    ran: list[str] = []
    monkeypatch.setattr(build_docs, "run_generators", lambda: ran.append("x") or [])
    monkeypatch.setattr(build_docs, "build_site", lambda site_dir: True)
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
