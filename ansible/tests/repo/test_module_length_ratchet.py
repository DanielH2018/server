"""The module-length and monkeypatch ratchets: the census, the git reads, and the tests.

`ansible/tests/_ratchet.py` holds the pure half — the caps, the allowlist parser, the two
comparisons and the patch counter — and its docstring is where the model and the heuristic's
blind spots are written down. `ansible/tests/_ratchet_census.py` holds the two ratchets and
the census of the tree that feeds them, so the `--tighten` writer can import the same census
this module asserts on. What is left here is the reads of the base, and the tests for all
three.

The base is `git merge-base HEAD origin/master`, never the live `origin/master` tip. CI tests a
PR's merge ref, whose first parent is the master commit the ref was built on, and
`tighten-ratchet-allowlists` lowers entries on master many times an hour. Against the live tip,
every entry master lowered after the ref was built reads as one this PR raised (#4009). The
merge base is that first parent on a merge ref, the fork point on a local branch, and HEAD
itself on master, where the PR already ran the comparison.

The comparison skips, naming which reason, when there is no base or when a list is not on the
base yet; a `git show` that fails for a path the base does track is a failure, not a skip.

Run: uv run pytest ansible/tests/repo/test_module_length_ratchet.py
"""

from collections.abc import Callable, Iterable, Mapping
from functools import cache
from subprocess import CompletedProcess

import pytest

from _helpers import REPO
from lib.git_testing import commit, git, init_repo
from _ratchet_census import (
    LENGTHS,
    PATCHES,
    line_counts,
    monkeypatch_counts,
    module_fixtures_by_dir,
    module_fixtures_for,
    run_git,
    tracked_python_files,
)
from _ratchet import (
    NON_TEST_CAP,
    TEST_CAP,
    Ratchet,
    cap_for,
    function_differs,
    function_source,
    parse_allowlist,
    raised_entries,
)

# One file per top-level tree that holds Python, asserted by name so the census cannot go
# quiet. A bare size floor would still pass if a whole tree stopped being enumerated.
CENSUS_MEMBERS = frozenset(
    {
        ".claude/hooks/_hook_common.py",
        "ansible/filter_plugins/toposort.py",
        "evals/trend.py",
        "scripts/lib/repo_paths.py",
    }
)

# The conftest fixture census finds its subject by globbing for `conftest.py`, so it has to
# name a member it must find: the one fixture in the tree that hands a test a first-party
# module. Without this the census reads empty the day that conftest is renamed, and every
# fixture-parameter patch goes back to counting 0 with nothing saying so.
MODULE_FIXTURE_MEMBERS = frozenset(
    {("ansible/roles/setup/gitops_deploy/tests", "gitops_deploy")}
)

# Changing any of these changes what the lists are allowed to contain, which is what lets a
# widened heuristic add the files it newly sees.
WHOLE_FILE_GUARDS = (
    "ansible/tests/_ratchet.py",
    "ansible/tests/_ratchet_census.py",
    "ansible/tests/repo/test_module_length_ratchet.py",
)

# `is_test_file` decides both the cap a path gets and which files the patch census covers, so
# it is a guard source. The rest of `_helpers.py` is not: many modules import that file, and
# comparing all of it would let an unrelated edit wave through a forbidden addition.
HELPERS = "ansible/tests/_helpers.py"
CAP_DECIDER = "is_test_file"

GUARD_SOURCES = (*WHOLE_FILE_GUARDS, HELPERS)


def base_of(run: Callable[..., CompletedProcess[str]]) -> str | None:
    """The commit the allowlists are compared against, or None when `origin/master` is absent."""
    found = run("merge-base", "HEAD", "origin/master")
    return found.stdout.strip() if found.returncode == 0 else None


@cache
def base() -> str | None:
    return base_of(run_git)


def _base_ref() -> str:
    """`base()` for a reader the caller has already skipped when there is none."""
    ref = base()
    if ref is None:
        raise RuntimeError("no merge base with origin/master in this checkout")
    return ref


def tracked_on_base(rel: str) -> bool:
    return run_git("cat-file", "-e", f"{_base_ref()}:{rel}").returncode == 0


def text_on_base(rel: str) -> str:
    """The file's content on the base. Raises when git cannot produce it."""
    shown = run_git("show", f"{_base_ref()}:{rel}")
    if shown.returncode:
        raise RuntimeError(
            f"git show {_base_ref()}:{rel} failed: {shown.stderr.strip()}"
        )
    return shown.stdout


def differs_from_base(rel: str) -> bool:
    return run_git("diff", "--quiet", _base_ref(), "--", rel).returncode != 0


def guard_differs_from_base() -> bool:
    """Whether this branch changes the rules, which is what lets it add a path to a list."""
    if any(differs_from_base(rel) for rel in WHOLE_FILE_GUARDS):
        return True
    if not tracked_on_base(HELPERS):
        return True
    return function_differs(
        text_on_base(HELPERS), (REPO / HELPERS).read_text(), CAP_DECIDER
    )


# ---------------------------------------------------------------- red-proof pairs


def test_a_module_outside_a_tests_directory_gets_the_non_test_cap():
    assert cap_for("scripts/docs/build_docs.py") == NON_TEST_CAP


def test_a_test_module_gets_the_test_cap():
    assert cap_for("ansible/tests/repo/test_adr_links.py") == TEST_CAP
    assert cap_for("scripts/deploy_tools/tests/_land_fakes.py") == TEST_CAP
    assert cap_for("scripts/docs/conftest.py") == TEST_CAP


def test_the_parser_accepts_a_clean_allowlist():
    text = "# a comment\n\nscripts/a.py 700\nscripts/b.py 610\n"
    assert parse_allowlist(text) == {"scripts/a.py": 700, "scripts/b.py": 610}


def test_the_parser_rejects_a_duplicate_path():
    with pytest.raises(ValueError, match="is listed twice"):
        parse_allowlist("scripts/a.py 700\nscripts/a.py 800\n")


def test_the_parser_rejects_a_malformed_line():
    with pytest.raises(ValueError, match="expected `<path> <max>`"):
        parse_allowlist("scripts/a.py\n")


def test_a_tree_within_its_caps_and_at_its_allowlist_is_clean():
    counts = {"scripts/a.py": 599, "scripts/big.py": 900}
    allow = {"scripts/big.py": 900}
    assert LENGTHS.violations(counts, allow) == []


def test_a_listed_module_that_shrank_below_its_entry_must_lower_the_line():
    """The gap between a file and its entry is regrowth headroom nothing else reports."""
    flagged = LENGTHS.violations({"scripts/big.py": 899}, {"scripts/big.py": 900})
    assert len(flagged) == 1
    assert "lower that line" in flagged[0] and "899" in flagged[0]


def test_a_listed_test_module_that_dropped_a_patch_must_lower_the_line():
    flagged = PATCHES.violations(
        {"scripts/tests/test_a.py": 2}, {"scripts/tests/test_a.py": 3}
    )
    assert len(flagged) == 1
    assert "lower that line" in flagged[0]


def test_an_unlisted_module_over_its_cap_is_flagged():
    flagged = LENGTHS.violations({"scripts/a.py": 601}, {})
    assert len(flagged) == 1
    assert "scripts/a.py" in flagged[0] and "601" in flagged[0]


def test_a_listed_module_that_grew_past_its_entry_is_flagged():
    flagged = LENGTHS.violations({"scripts/a.py": 951}, {"scripts/a.py": 950})
    assert len(flagged) == 1
    assert "950" in flagged[0]


def test_a_listed_module_back_under_its_cap_must_leave_the_allowlist():
    flagged = LENGTHS.violations({"scripts/a.py": 400}, {"scripts/a.py": 950})
    assert len(flagged) == 1
    assert "remove it from" in flagged[0]


def test_a_listed_path_that_no_longer_exists_is_flagged():
    flagged = LENGTHS.violations({}, {"scripts/gone.py": 950})
    assert len(flagged) == 1
    assert "no tracked file" in flagged[0]


def test_a_test_module_with_no_module_patches_is_clean():
    assert PATCHES.violations({"scripts/tests/test_a.py": 0}, {}) == []


def test_a_test_module_with_an_unlisted_module_patch_is_flagged():
    flagged = PATCHES.violations({"scripts/tests/test_a.py": 1}, {})
    assert len(flagged) == 1
    assert "seam" in flagged[0]


def test_an_allowlist_that_only_falls_is_clean():
    old = {"scripts/a.py": 900, "scripts/b.py": 700}
    assert raised_entries(old, {"scripts/a.py": 800}, "list.txt") == []


def test_a_raised_entry_is_flagged():
    flagged = raised_entries({"scripts/a.py": 900}, {"scripts/a.py": 901}, "list.txt")
    assert len(flagged) == 1
    assert "up from 900" in flagged[0]


def test_adding_a_path_master_already_tracks_is_flagged():
    flagged = raised_entries({}, {"scripts/old.py": 900}, "list.txt")
    assert len(flagged) == 1
    assert "added to list.txt" in flagged[0]


def test_adding_a_path_master_does_not_track_is_clean():
    """A renamed or brand-new file has to be able to enter the list."""
    added = {"scripts/new.py": 900}
    assert (
        raised_entries({}, added, "list.txt", untracked_on_master=["scripts/new.py"])
        == []
    )


def test_adding_a_path_is_clean_when_this_commit_changes_the_guard():
    """A widened heuristic finds patches that were always there; they must be listable."""
    added = {"scripts/old.py": 900}
    assert raised_entries({}, added, "list.txt", guard_changed=True) == []


def test_a_raised_entry_is_clean_when_this_commit_changes_the_guard():
    """A widening finds patches that were always there in an already-listed file too.

    The pair is `test_a_raised_entry_is_flagged` above: without a guard change, a rise fails.
    """
    clean = raised_entries(
        {"scripts/a.py": 900}, {"scripts/a.py": 901}, "list.txt", guard_changed=True
    )
    assert clean == []


_HELPERS_BEFORE = (
    "X = 1\n\n\ndef is_test_file(p):\n    return p.name.startswith('test_')\n"
)


def test_an_edit_elsewhere_in_the_file_leaves_the_watched_function_unchanged():
    """Many modules import `_helpers`; only `is_test_file` decides a cap."""
    other_edit = _HELPERS_BEFORE.replace("X = 1", "X = 2\nY = 3")
    assert not function_differs(_HELPERS_BEFORE, other_edit, CAP_DECIDER)


def test_a_changed_function_body_is_a_changed_guard():
    widened = _HELPERS_BEFORE.replace(
        "return p.name.startswith('test_')",
        "return p.name.startswith('test_') or 'tests' in p.parts",
    )
    assert function_differs(_HELPERS_BEFORE, widened, CAP_DECIDER)
    assert function_differs(_HELPERS_BEFORE, "X = 1\n", CAP_DECIDER)


def test_a_conftest_fixture_reaches_a_test_below_it_but_not_a_sibling_tree():
    by_dir = {"a/tests": frozenset({"mod"})}
    assert module_fixtures_for("a/tests/test_x.py", by_dir) == {"mod"}
    assert module_fixtures_for("a/tests/deep/test_x.py", by_dir) == {"mod"}
    assert module_fixtures_for("b/tests/test_x.py", by_dir) == frozenset()


# ---------------------------------------------------------------- the live tree


def test_the_census_reaches_every_tree_that_holds_python():
    """Without this, every assertion below passes on a census that has gone quiet."""
    counts = line_counts()
    assert CENSUS_MEMBERS <= set(counts), CENSUS_MEMBERS - set(counts)
    assert sum(1 for rel in counts if cap_for(rel) == TEST_CAP) >= 100
    assert sum(1 for rel in counts if cap_for(rel) == NON_TEST_CAP) >= 100


def test_the_module_fixture_census_finds_the_fixtures_it_names():
    """A glob census that goes empty would silently return every count to zero."""
    by_dir = module_fixtures_by_dir(tracked_python_files())
    found = {(d, name) for d, names in by_dir.items() for name in names}
    assert MODULE_FIXTURE_MEMBERS <= found, MODULE_FIXTURE_MEMBERS - found


def test_no_module_is_longer_than_its_cap_or_its_allowlist_entry():
    offenders = LENGTHS.violations(line_counts(), LENGTHS.allowlist())
    assert not offenders, "\n".join(offenders)


def test_no_test_module_patches_more_modules_than_its_allowlist_entry():
    offenders = PATCHES.violations(monkeypatch_counts(), PATCHES.allowlist())
    assert not offenders, "\n".join(offenders)


def _missing_sources(paths: Iterable[str]) -> list[str]:
    return [rel for rel in paths if not (REPO / rel).is_file()]


def test_every_guard_source_exists_at_the_path_named():
    """`differs_from_base` answers False for a path absent on both sides.

    A renamed guard source would therefore read as unchanged, and the exemption that lets a
    widened rule add entries would be off with nothing saying so.
    """
    assert _missing_sources(GUARD_SOURCES) == []
    assert function_source((REPO / HELPERS).read_text(), CAP_DECIDER), (
        f"{HELPERS} no longer defines {CAP_DECIDER}, so comparing that function against "
        f"the base compares None with None and the exemption never fires."
    )


def test_a_guard_source_that_does_not_exist_is_flagged():
    assert _missing_sources(("no/such.py",)) == ["no/such.py"]


def test_the_base_read_returns_content_for_a_path_the_base_tracks():
    """The comparison below skips until the lists reach the base; this keeps the read proved."""
    if base() is None:
        pytest.skip("origin/master is not fetched in this checkout")
    assert tracked_on_base("ansible/tests/_helpers.py")
    assert text_on_base("ansible/tests/_helpers.py").startswith('"""')
    assert not tracked_on_base("ansible/tests/no_such_file.py")


def test_the_base_read_raises_rather_than_returning_empty_for_an_unknown_path():
    """A failed read must fail the comparison, not quietly look like an empty allowlist."""
    if base() is None:
        pytest.skip("origin/master is not fetched in this checkout")
    with pytest.raises(RuntimeError):
        text_on_base("ansible/tests/no_such_file.py")


def test_an_entry_master_lowered_after_the_merge_ref_was_built_is_not_flagged(tmp_path):
    """The #4009 race: CI's merge ref is built, then master lowers an entry the PR never touched.

    The live tip flags the untouched entry as raised, which is the red half. The base resolves
    to the merge ref's first parent, where the entry still has the PR's number.
    """
    repo = init_repo(tmp_path)
    built_on = commit(repo, "base", **{"list.txt": "scripts/a.py 626\n"})
    git(repo, "checkout", "-q", "-b", "pr")
    commit(repo, "the PR", **{"other.py": "x\n"})
    git(repo, "checkout", "-q", "--detach", built_on)
    git(repo, "merge", "-q", "--no-ff", "--no-gpg-sign", "-m", "merge ref", "pr")
    merge_ref = git(repo, "rev-parse", "HEAD").stdout.strip()
    git(repo, "checkout", "-q", "-b", "later", built_on)
    tip = commit(repo, "tighten", **{"list.txt": "scripts/a.py 613\n"})
    git(repo, "update-ref", "refs/remotes/origin/master", tip)
    git(repo, "checkout", "-q", "--detach", merge_ref)

    def read(ref: str) -> dict[str, int]:
        return parse_allowlist(git(repo, "show", f"{ref}:list.txt").stdout)

    head = read("HEAD")
    assert raised_entries(read("origin/master"), head, "list.txt")
    assert base_of(lambda *a: git(repo, *a, check=False)) == built_on
    assert raised_entries(read(built_on), head, "list.txt") == []


@pytest.mark.parametrize("ratchet", [LENGTHS, PATCHES], ids=lambda r: r.path.name)
def test_no_allowlist_entry_rose_against_the_merge_base(ratchet: Ratchet):
    """A commit's own counts cannot see a diff that grows a file and its entry together."""
    rel = ratchet.path.relative_to(REPO).as_posix()
    if base() is None:
        pytest.skip(
            "origin/master is not fetched, so there is no merge base to compare against. "
            "`prek run --all-files` runs this locally, where the ref exists."
        )
    if not tracked_on_base(rel):
        pytest.skip(
            f"{rel} is not on the merge base yet, so there is nothing to compare"
        )
    new = ratchet.allowlist()
    offenders = raised_entries(
        parse_allowlist(text_on_base(rel)),
        new,
        ratchet.path.name,
        untracked_on_master=[p for p in new if not tracked_on_base(p)],
        guard_changed=guard_differs_from_base(),
    )
    assert not offenders, "\n".join(offenders)


def test_the_live_length_ratchet_flags_a_lowered_entry():
    """The pairs above prove the comparison; this proves the census feeds the real list."""
    _assert_a_lowered_entry_is_flagged(LENGTHS, line_counts())


def test_the_live_monkeypatch_ratchet_flags_a_lowered_entry():
    _assert_a_lowered_entry_is_flagged(PATCHES, monkeypatch_counts())


def _assert_a_lowered_entry_is_flagged(ratchet: Ratchet, counts: Mapping[str, int]):
    allow = ratchet.allowlist()
    if not allow:
        pytest.skip(f"{ratchet.path.name} is empty — every file is under its cap")
    listed = next(iter(allow))
    assert ratchet.violations(counts, {**allow, listed: allow[listed] - 1})


def test_the_committed_allowlists_are_sorted():
    """Sorted, one line per file, is what makes parallel PRs merge cleanly."""
    for ratchet in (LENGTHS, PATCHES):
        listed = list(ratchet.allowlist())
        assert listed == sorted(listed), ratchet.path.name
