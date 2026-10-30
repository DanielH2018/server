"""M6 records each batch's results in its own file, so parallel soak and cutover PRs merge cleanly.

Every soak and cutover PR once wrote the same status block of `m6-retire-kuma.md`, and three
PRs on 2026-10-30 each needed a hand merge of master (#6656). The page now transcludes one
file per batch from `m6-status/`, and every include line is already written, so a PR that
records a result edits only its batch's file.

Run: uv run pytest ansible/tests/repo/test_m6_status_records.py
"""

import re
import shutil

from _markdown_section import section
from lib.git_testing import commit, git, init_repo
from lib.repo_paths import REPO

DOC = REPO / "docs" / "target-architecture" / "monitoring" / "m6-retire-kuma.md"
DOCS = REPO / "docs"
STATUS_DIR = DOC.parent / "m6-status"

_INCLUDE = re.compile(
    r'^--8<-- "(target-architecture/monitoring/m6-status/[^"]+\.md)"$', re.M
)
_BATCH_ROW = re.compile(r"^\| ([AB]\d+[a-z]?) ", re.M)
_PR_ROW = re.compile(r"^\| ([BC]-\d+)[a-z]? ", re.M)

# Batches that held a record when the status block was split, and so must still be found.
RECORDED = {"a3", "a4", "a5", "b8", "pr-b-1", "pr-c-0"}


def _includes() -> list[str]:
    return _INCLUDE.findall(section(DOC.read_text(), "## Status by batch"))


def _slug(include: str) -> str:
    return include.rsplit("/", 1)[1].removesuffix(".md")


def _table_slugs() -> list[str]:
    """The record file each batch-table row and Phase B/C PR row needs, in table order."""
    text = DOC.read_text()
    batches = _BATCH_ROW.findall(section(text, "### Phase A — drain the producers"))
    prs = _PR_ROW.findall(section(text, "## Steps, as PRs"))
    slugs = [b.lower() for b in batches] + [f"pr-{p.lower()}" for p in prs]
    return list(dict.fromkeys(slugs))


def test_every_table_row_has_a_transcluded_record_in_table_order():
    table = _table_slugs()
    assert {"a1", "a3", "b6a", "b9", "pr-b-1", "pr-c-7"} <= set(table), table
    included = [_slug(i) for i in _includes()]
    missing = [s for s in table if s not in included]
    assert not missing, f"batches with no transcluded record file: {missing}"
    in_table = [s for s in included if s in table]
    assert in_table == table, f"includes leave table order: {in_table}"


def test_every_include_resolves_and_every_record_file_is_included_once():
    includes = _includes()
    assert len(includes) == len(set(includes)), "a record file is transcluded twice"
    assert all((DOCS / i).is_file() for i in includes)
    on_disk = {p.stem for p in STATUS_DIR.glob("*.md")}
    assert on_disk == {_slug(i) for i in includes}
    assert RECORDED <= on_disk
    for slug in RECORDED:
        text = (STATUS_DIR / f"{slug}.md").read_text()
        assert "Nothing recorded." not in text, f"{slug}.md lost its record"


def _record_branch(repo, branch: str, path: str, record: str) -> None:
    git(repo, "checkout", "-q", "-b", branch, "master")
    target = repo / path
    commit(
        repo, branch, **{path: target.read_text().replace("Nothing recorded.", record)}
    )


def test_two_batches_recording_side_by_side_merge_without_conflict(tmp_path):
    repo = init_repo(tmp_path / "repo")
    shutil.copytree(DOCS / "target-architecture", repo / "target-architecture")
    commit(repo, "base")
    # Adjacent includes are the worst case: their records sit next to each other on the page.
    first, second = _includes()[:2]
    _record_branch(repo, "first", first, "Soak read: first batch.")
    _record_branch(repo, "second", second, "Soak read: second batch.")
    merged = git(repo, "merge-tree", "--write-tree", "first", "second", check=False)
    assert merged.returncode == 0, merged.stdout


def test_two_records_written_into_one_shared_block_conflict(tmp_path):
    """The red half: the merge check fails for the shape the per-batch files replace."""
    repo = init_repo(tmp_path / "repo")
    commit(
        repo, "base", **{"doc.md": "# M6\n\nStatus: IN PROGRESS\n\nDepends on: M2\n"}
    )
    shared = "Status: IN PROGRESS\n"
    for branch, record in (("first", "first batch"), ("second", "second batch")):
        git(repo, "checkout", "-q", "-b", branch, "master")
        text = (
            (repo / "doc.md")
            .read_text()
            .replace(shared, f"{shared}\nSoak read: {record}.\n")
        )
        commit(repo, branch, **{"doc.md": text})
    merged = git(repo, "merge-tree", "--write-tree", "first", "second", check=False)
    assert merged.returncode == 1, merged.stdout
