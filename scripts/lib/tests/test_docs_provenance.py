"""The provenance banner is the only staleness signal generated docs carry.

There is no monitor and no deadman on the docs cron. A stopped cron surfaces as an
old date on the page, so these assertions are load-bearing rather than cosmetic.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import yaml

from docs_provenance import (
    finish_generator,
    generated_banner,
    head_sha,
    md_cell,
    write_if_body_changed,
)

FIXED = dt.datetime(2026, 8, 24, 14, 30, tzinfo=dt.timezone.utc)


def test_banner_carries_the_timestamp():
    banner = generated_banner(
        "scripts/docs/service_catalog.py", when=FIXED, sha="abc1234"
    )
    assert "2026-08-24" in banner
    assert "14:30" in banner


def test_banner_carries_the_source_and_sha():
    banner = generated_banner(
        "scripts/docs/service_catalog.py", when=FIXED, sha="abc1234"
    )
    assert "scripts/docs/service_catalog.py" in banner
    assert "abc1234" in banner


def test_banner_warns_against_hand_editing():
    """The hook is what enforces this, but the page must say so too.

    Someone reading the rendered page has no view of the prek config.
    """
    banner = generated_banner(
        "scripts/docs/service_catalog.py", when=FIXED, sha="abc1234"
    )
    assert "do not edit" in banner.lower()


def test_banner_opens_with_yaml_frontmatter():
    banner = generated_banner(
        "scripts/docs/service_catalog.py", when=FIXED, sha="abc1234"
    )
    lines = banner.splitlines()
    assert lines[0] == "---"
    assert "---" in lines[1:], "frontmatter block is never closed"


def test_banner_frontmatter_parses_as_yaml():
    banner = generated_banner(
        "scripts/docs/service_catalog.py", when=FIXED, sha="abc1234"
    )
    body = banner.split("---")[1]
    meta = yaml.safe_load(body)
    assert meta["generated_from"] == "scripts/docs/service_catalog.py"
    assert meta["generated_sha"] == "abc1234"


def test_head_sha_falls_back_when_git_is_unavailable(tmp_path):
    """A non-repo directory yields 'unknown', never a traceback.

    The cron runs unattended. A generator that dies because git moved is a worse
    failure than a page whose provenance line reads 'unknown'.
    """
    assert head_sha(tmp_path) == "unknown"


def test_head_sha_ignores_an_inherited_git_dir(tmp_path, monkeypatch):
    """`cwd=` alone does not scope a git call — GIT_DIR beats it.

    This only reproduces inside a git hook, which exports GIT_DIR and GIT_WORK_TREE
    pointing at the repo running the hook. The standalone test above passed while
    head_sha() was reporting the real SHA for every path it was handed; the
    pre-commit run is what caught it. Set explicitly here so it cannot regress
    somewhere the hook does not run.
    """
    monkeypatch.setenv("GIT_DIR", str(Path(__file__).resolve().parents[3] / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(Path(__file__).resolve().parents[3]))
    assert head_sha(tmp_path) == "unknown"


def test_head_sha_reads_the_real_repo():
    sha = head_sha(Path(__file__).resolve().parents[3])
    assert sha != "unknown"
    assert len(sha) >= 7


# ── write_if_body_changed ──────────────────────────────────────────────────────────────


def _page(body: str, stamp: str) -> str:
    return f"---\ngenerated_at: {stamp}\n---\n\n{body}"


def test_writes_when_the_file_does_not_exist(tmp_path):
    target = tmp_path / "new.md"
    assert write_if_body_changed(target, _page("hello", "A")) is True
    assert target.is_file()


def test_writes_when_the_body_changed(tmp_path):
    target = tmp_path / "p.md"
    target.write_text(_page("old", "A"))
    assert write_if_body_changed(target, _page("new", "B")) is True
    assert "new" in target.read_text()


def test_does_not_write_when_only_the_stamp_changed(tmp_path):
    """The assertion the whole cron depends on.

    Without it every run rewrites every page, the cron's `git diff --cached` is
    never empty, and twice a day becomes ~730 commits a year -- each one a master
    CI run and a tick fast-forward, for no content change at all.
    """
    target = tmp_path / "p.md"
    target.write_text(_page("same", "A"))
    before = target.read_text()
    assert write_if_body_changed(target, _page("same", "B")) is False
    assert target.read_text() == before, "file was rewritten despite an identical body"


def test_a_horizontal_rule_in_the_body_does_not_truncate_the_comparison(tmp_path):
    """`---` is also Markdown for a horizontal rule, and generated pages use it.

    Splitting on every '---' rather than the first two would compare only the text
    above the first rule, so a change below one would be silently dropped.
    """
    target = tmp_path / "p.md"
    target.write_text(_page("intro\n\n---\n\nold tail", "A"))
    assert write_if_body_changed(target, _page("intro\n\n---\n\nnew tail", "A")) is True
    assert "new tail" in target.read_text()


def test_creates_parent_directories(tmp_path):
    target = tmp_path / "deep" / "nested" / "p.md"
    assert write_if_body_changed(target, _page("x", "A")) is True


def test_md_cell_escapes_a_pipe():
    assert md_cell("a | b") == "a \\| b"


def test_md_cell_leaves_a_clean_value_alone():
    assert md_cell("plain text") == "plain text"


def test_finish_generator_writes_and_reports_wrote(tmp_path, capsys):
    out = tmp_path / "page.md"
    rc = finish_generator(
        "gen_x", out, [1, 2, 3], lambda rows: f"body {len(rows)}\n", "thing"
    )
    assert rc == 0
    assert out.read_text() == "body 3\n"
    assert capsys.readouterr().out.strip() == f"gen_x: 3 thing(s), wrote {out}"


def test_finish_generator_reports_unchanged_on_a_second_run(tmp_path, capsys):
    out = tmp_path / "page.md"
    finish_generator("gen_x", out, [1], lambda rows: "same\n", "thing")
    capsys.readouterr()
    finish_generator("gen_x", out, [1], lambda rows: "same\n", "thing")
    assert capsys.readouterr().out.strip() == f"gen_x: 1 thing(s), unchanged {out}"
