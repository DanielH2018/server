"""doc_freshness: the git-log parse, the path extractor, the resolver, and the moved rule."""

from __future__ import annotations

import textwrap

from lib import doc_freshness as f

TRACKED = {
    "docs/deploying.md",
    "docs/adr/0011-x.md",
    "scripts/deploy.sh",
    "ansible/roles/setup/k3s/defaults/main.yml",
    "ansible/roles/setup/gitops_deploy/files/deploy_logic.py",
    "ansible/roles/setup/gitops_deploy/tests/test_deploy_logic.py",
    "scripts/x/dup.py",
    "scripts/y/dup.py",
}


# --- parse_change_dates ---------------------------------------------------------------


def test_the_first_date_a_path_appears_under_is_its_latest():
    log = textwrap.dedent("""\
        2026-09-02

        a.py
        b.py
        2026-08-01

        a.py
        c.py
        """)
    assert f.parse_change_dates(log) == {
        "a.py": "2026-09-02",
        "b.py": "2026-09-02",
        "c.py": "2026-08-01",
    }


def test_an_empty_log_dates_nothing():
    assert f.parse_change_dates("") == {}


# --- named_paths -----------------------------------------------------------------------


def test_a_path_inside_a_command_span_is_found_with_its_suffix_dropped():
    text = "run `./scripts/deploy.sh --tags x`, see `deploy_logic.py:458` and `t.py::test_a`"
    assert f.named_paths(text) == ["scripts/deploy.sh", "deploy_logic.py", "t.py"]


def test_a_fenced_block_counts_as_code():
    text = "```bash\nuv run python scripts/deploy.sh\n```\nplain scripts/no.py here\n"
    assert f.named_paths(text) == ["scripts/deploy.sh"]


def test_a_host_port_pair_is_not_a_path():
    assert f.named_paths("dial `10.0.0.240:51820` and `127.0.0.1:3100`") == []


def test_prose_outside_code_is_ignored():
    assert f.named_paths("edit scripts/deploy.sh by hand") == []


# --- resolve ---------------------------------------------------------------------------


def test_a_doc_relative_name_resolves_first():
    assert f.resolve("0011-x.md", "docs/adr/index.md", TRACKED) == "docs/adr/0011-x.md"


def test_a_repo_relative_name_resolves():
    assert (
        f.resolve("scripts/deploy.sh", "docs/deploying.md", TRACKED)
        == "scripts/deploy.sh"
    )


def test_a_unique_suffix_resolves_without_its_prefix():
    got = f.resolve("k3s/defaults/main.yml", "docs/deploying.md", TRACKED)
    assert got == "ansible/roles/setup/k3s/defaults/main.yml"


def test_an_ambiguous_suffix_resolves_to_nothing():
    assert f.resolve("dup.py", "docs/deploying.md", TRACKED) is None


def test_a_basename_alone_is_a_suffix_only_when_unique():
    assert f.resolve("deploy_logic.py", "docs/deploying.md", TRACKED) == (
        "ansible/roles/setup/gitops_deploy/files/deploy_logic.py"
    )


def test_a_name_nothing_tracks_resolves_to_nothing():
    assert f.resolve("scripts/gone.py", "docs/deploying.md", TRACKED) is None


# --- page_freshness: the moved rule ---------------------------------------------------


def test_a_source_changed_after_the_page_is_moved():
    dates = {
        "docs/deploying.md": "2026-08-01",
        "scripts/deploy.sh": "2026-09-01",
        "ansible/roles/setup/k3s/defaults/main.yml": "2026-07-01",
    }
    text = "`scripts/deploy.sh` reads `k3s/defaults/main.yml`"
    got = f.page_freshness("docs/deploying.md", text, dates, TRACKED)
    assert got.changed == "2026-08-01"
    assert [p for p, _ in got.sources] == [
        "scripts/deploy.sh",
        "ansible/roles/setup/k3s/defaults/main.yml",
    ]
    assert got.moved == [("scripts/deploy.sh", "2026-09-01")]


def test_a_source_changed_the_same_day_is_not_moved():
    dates = {"docs/deploying.md": "2026-08-01", "scripts/deploy.sh": "2026-08-01"}
    got = f.page_freshness("docs/deploying.md", "`scripts/deploy.sh`", dates, TRACKED)
    assert got.moved == []


def test_a_page_naming_itself_does_not_count_itself():
    dates = {"docs/deploying.md": "2026-08-01"}
    got = f.page_freshness("docs/deploying.md", "`docs/deploying.md`", dates, TRACKED)
    assert got.sources == []


def test_a_source_git_has_no_date_for_is_never_moved():
    got = f.page_freshness("docs/deploying.md", "`scripts/deploy.sh`", {}, TRACKED)
    assert got.sources == [("scripts/deploy.sh", "")]
    assert got.moved == []


# --- is_hand_written -------------------------------------------------------------------


def test_a_generated_page_is_told_by_its_frontmatter():
    text = "---\ngenerated_from: scripts/docs/x.py\n---\n\n# X\n"
    assert not f.is_hand_written("docs/reference/x.md", text)
    assert not f.is_hand_written("docs/x.md", text)


def test_a_page_that_mentions_the_marker_in_prose_is_still_hand_written():
    text = "# About\n\nEvery generated page carries `generated_from:` in its frontmatter.\n"
    assert f.is_hand_written("docs/about.md", text)


def test_the_archive_the_assets_and_the_template_are_skipped():
    assert not f.is_hand_written("docs/archive/old.md", "# old")
    assert not f.is_hand_written("docs/assets/generated/fragments/a.md", "x")
    assert not f.is_hand_written("docs/adr/template.md", "# form")
    assert f.is_hand_written("docs/adr/0011-x.md", "# a decision")


def test_a_non_doc_is_never_hand_written():
    assert not f.is_hand_written("CLAUDE.md", "# x")
    assert not f.is_hand_written("docs/assets/extra.css", "body {}")


# --- against the real tree -----------------------------------------------------------------


def test_the_survey_finds_the_live_pages():
    from lib.repo_paths import REPO

    pages = f.survey(REPO)
    names = {p.page for p in pages}
    assert "docs/deploying.md" in names
    assert "docs/index.md" in names
    assert not any(
        n.startswith("docs/reference/") and n != "docs/reference/topology.md"
        for n in names
    )
    assert not any(n.startswith("docs/archive/") for n in names)
    assert len(pages) >= 30
    # Nearly every page names something; a walk that found no sources anywhere is broken.
    assert sum(len(p.sources) for p in pages) >= 100
