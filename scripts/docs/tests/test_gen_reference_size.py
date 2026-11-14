"""docs.reference.size: the repo-size page, from a scratch repository with backdated history.

The scratch history has three commits, 120, 60 and 5 days before `_NOW`. The generator takes
the repository as `repo=` and the clock as `now=`. Whether it measures "30 and 90 days earlier"
from `now` or from HEAD's commit date, the 60-day commit is the 30-day value and the 120-day
commit is the 90-day value.
"""

import importlib
import importlib.util
import re
from datetime import datetime, timedelta, timezone

import build_docs
from lib.git_testing import git, init_repo, scrub_process_git_env, scrubbed_env

MODULE = "docs.reference.size"
SCRIPT = "scripts/docs/reference/size.py"
PAGE = "docs/reference/size.md"
_NOW = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)

# Each state adds to the one before; a value is the file's line count.
OLD = {
    "k8s/a/x.yml": 17,
    "k8s/b/y.yml": 24,
    "k8s/a/tests/test_x.py": 11,
    "docs/one.md": 33,
    "CLAUDE.md": 12,
    ".claude/rules/r1.md": 19,
    ".claude/rules/r2.md": 26,
    ".claude/agents/a1.md": 14,
    ".claude/skills/s1/SKILL.md": 21,
    ".claude/hooks/h1.py": 15,
}
MID = OLD | {
    "scripts/tool.py": 47,
    "scripts/tests/test_tool.py": 38,
    "scripts/tests/test_more.py": 29,
    "k8s/c/z.yml": 56,
    "k8s/c/CLAUDE.md": 18,
    ".claude/rules/r3.md": 31,
    ".claude/agents/a2.md": 22,
    ".claude/skills/s2/SKILL.md": 27,
    ".claude/hooks/h2.py": 35,
    ".claude/hooks/h3.py": 16,
}
NEW = MID | {
    "scripts/more.py": 63,
    "scripts/tests/test_new.py": 44,
    "scripts/lib/util.py": 71,
    "k8s/d/w.yml": 82,
    "docs/two.md": 49,
    "docs/CLAUDE.md": 13,
    ".claude/rules/r4.md": 37,
    ".claude/rules/r5.md": 28,
    ".claude/agents/a3.md": 41,
    ".claude/agents/a4.md": 43,
    ".claude/agents/a5.md": 46,
    ".claude/agents/a6.md": 39,
    ".claude/skills/s3/SKILL.md": 25,
    ".claude/skills/s4/SKILL.md": 34,
    ".claude/hooks/h4.py": 52,
    ".claude/hooks/h5.py": 53,
    ".claude/hooks/h6.py": 54,
    ".claude/hooks/h7.py": 57,
}
TREES = ("k8s", "scripts", "docs", ".claude")
KINDS = {
    "rule": re.compile(r"\.claude/rules/[^/]+\.md"),
    "skill": re.compile(r"\.claude/skills/[^/]+/SKILL\.md"),
    "agent": re.compile(r"\.claude/agents/[^/]+\.md"),
    "hook": re.compile(r"\.claude/hooks/[^/]+\.py"),
}


def _tree_files(state, tree):
    return sum(1 for path in state if path.startswith(f"{tree}/"))


def _tree_lines(state, tree):
    return sum(n for path, n in state.items() if path.startswith(f"{tree}/"))


def _test_files(state):
    return sum(1 for path in state if re.fullmatch(r"(.*/)?test_[^/]*\.py", path))


def _instruction_lines(state):
    return sum(
        n
        for path, n in state.items()
        if path.startswith(".claude/") or re.fullmatch(r"(.*/)?CLAUDE\.md", path)
    )


def _kind_count(state, kind):
    return sum(1 for path in state if KINDS[kind].fullmatch(path))


def _commit_state(repo, state, days_ago):
    for rel, n in state.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("".join(f"{rel} {i}\n" for i in range(n)))
    git(repo, "add", "-A")
    stamp = (_NOW - timedelta(days=days_ago)).isoformat()
    env = scrubbed_env(GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
    git(repo, "commit", "-q", "-m", f"{days_ago}d", "--no-gpg-sign", env=env)


def _page(tmp_path, monkeypatch):
    """Generate the size page for the scratch repository and return its text."""
    assert importlib.util.find_spec(MODULE) is not None, f"{SCRIPT} does not exist"
    size = importlib.import_module(MODULE)
    scrub_process_git_env(monkeypatch)
    repo = init_repo(tmp_path / "repo")
    _commit_state(repo, OLD, 120)
    _commit_state(repo, MID, 60)
    _commit_state(repo, NEW, 5)
    out = tmp_path / "size.md"
    assert size.main(["--out", str(out)], repo=repo, now=_NOW) == 0
    return out.read_text()


def _first_cell(row):
    cell = row.strip().strip("|").split("|")[0]
    cell = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", cell)
    return re.sub(r"[`*]", "", cell).strip()


def _numbers(row):
    row = re.sub(r"\d{4}-\d{2}-\d{2}", "", row)
    return {int(m.replace(",", "")) for m in re.findall(r"\d[\d,]*", row)}


def _rows_numbers(page, match):
    rows = [ln for ln in page.splitlines() if ln.lstrip().startswith("|")]
    found = [ln for ln in rows if match(_first_cell(ln))]
    assert found, "no table row matches"
    return set().union(*(_numbers(ln) for ln in found))


def _tree_numbers(page, tree):
    return _rows_numbers(page, lambda cell: cell.rstrip("/") == tree)


def _measure_numbers(page, word):
    return _rows_numbers(
        page,
        lambda cell: word in cell.lower() and cell.rstrip("/") not in TREES,
    )


def test_build_docs_writes_the_size_page_under_docs_reference():
    assert [argv[0] for argv, out in build_docs.GENERATORS if out == PAGE] == [SCRIPT]


def test_the_page_carries_the_provenance_banner(tmp_path, monkeypatch):
    assert _page(tmp_path, monkeypatch).startswith(f"---\ngenerated_from: {SCRIPT}\n")


def test_each_top_level_tree_shows_its_files_and_lines(tmp_path, monkeypatch):
    page = _page(tmp_path, monkeypatch)
    for tree in TREES:
        numbers = _tree_numbers(page, tree)
        assert _tree_files(NEW, tree) in numbers, tree
        assert _tree_lines(NEW, tree) in numbers, tree


def test_each_tree_shows_its_files_and_lines_30_and_90_days_earlier(
    tmp_path, monkeypatch
):
    page = _page(tmp_path, monkeypatch)
    for tree in ("k8s", ".claude"):
        numbers = _tree_numbers(page, tree)
        for state in (MID, OLD):
            assert _tree_files(state, tree) in numbers, tree
            assert _tree_lines(state, tree) in numbers, tree
    assert {_tree_files(MID, "scripts"), _tree_lines(MID, "scripts")} <= (
        _tree_numbers(page, "scripts")
    )


def test_test_files_show_today_and_30_and_90_days_earlier(tmp_path, monkeypatch):
    numbers = _measure_numbers(_page(tmp_path, monkeypatch), "test")
    assert {_test_files(NEW), _test_files(MID), _test_files(OLD)} <= numbers


def test_instruction_lines_count_every_claude_md_and_dot_claude(tmp_path, monkeypatch):
    numbers = _measure_numbers(_page(tmp_path, monkeypatch), "instruction")
    assert {
        _instruction_lines(NEW),
        _instruction_lines(MID),
        _instruction_lines(OLD),
    } <= numbers


def test_rules_skills_agents_and_hooks_are_counted_with_their_history(
    tmp_path, monkeypatch
):
    page = _page(tmp_path, monkeypatch)
    for kind in KINDS:
        numbers = _measure_numbers(page, kind)
        assert {_kind_count(s, kind) for s in (NEW, MID, OLD)} <= numbers, kind


def test_a_history_shorter_than_90_days_reads_as_no_commit(tmp_path, monkeypatch):
    size = importlib.import_module(MODULE)
    scrub_process_git_env(monkeypatch)
    repo = init_repo(tmp_path / "repo")
    _commit_state(repo, MID, 60)
    out = tmp_path / "size.md"
    assert size.main(["--out", str(out)], repo=repo, now=_NOW) == 0
    page = out.read_text()
    assert "*90 days earlier* has no commit" in page
    assert _tree_lines(MID, "k8s") in _tree_numbers(page, "k8s")
