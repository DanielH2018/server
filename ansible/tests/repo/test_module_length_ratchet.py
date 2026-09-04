"""The module-length and monkeypatch ratchets: the census, the git reads, and the tests.

`ansible/tests/_ratchet.py` holds the pure half — the caps, the allowlist parser, the two
comparisons and the patch counter — and its docstring is where the model and the heuristic's
blind spots are written down. This module reads the tree and `origin/master` and feeds them
in.

The comparison against `origin/master` needs that ref. It skips, naming which reason, when the
ref is not fetched or when a list is not on master yet; a `git show` that fails for a path
master does track is a failure, not a skip. Locally the ref is only as fresh as the last
`git fetch`, so a stale one compares against older numbers — which can only make the check
lenient, never wrong in the failing direction.

Run: uv run pytest ansible/tests/repo/test_module_length_ratchet.py
"""

import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

from _helpers import REPO, is_test_file
from _ratchet import (
    NON_TEST_CAP,
    TEST_CAP,
    Ratchet,
    cap_for,
    count_module_patches,
    first_party_module_names,
    parse_allowlist,
    raised_entries,
)

HERE = REPO / "ansible" / "tests" / "repo"

# One file per top-level tree that holds Python, asserted by name so the census cannot go
# quiet. A bare size floor would still pass if a whole tree stopped being enumerated.
CENSUS_MEMBERS = frozenset(
    {
        ".claude/hooks/_hook_common.py",
        "ansible/filter_plugins/toposort.py",
        "evals/harness_metrics.py",
        "scripts/lib/repo_paths.py",
    }
)

# Changing any of these changes what the lists are allowed to contain, which is what lets a
# widened heuristic add the files it newly sees. `_helpers.py` is here because its
# `is_test_file` decides both the cap a path gets and which files the patch census covers.
GUARD_SOURCES = (
    "ansible/tests/_helpers.py",
    "ansible/tests/_ratchet.py",
    "ansible/tests/repo/test_module_length_ratchet.py",
)

LENGTHS = Ratchet(
    path=HERE / "module_length_allowlist.txt",
    unit="lines",
    remedy="Split it: docs/python-code-organization.md says where the pieces go.",
    cap_of=cap_for,
)

PATCHES = Ratchet(
    path=HERE / "monkeypatch_allowlist.txt",
    unit="monkeypatch.setattr calls on a first-party module",
    remedy=(
        "Give the module under test a seam instead — a frozen dataclass of injectable "
        "boundaries, as in scripts/deploy_tools/land_lib/tools.py with its fakes in "
        "scripts/deploy_tools/tests/_land_fakes.py."
    ),
    cap_of=lambda rel: 0,
)


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=REPO, capture_output=True, text=True, check=False
    )


def master_is_fetched() -> bool:
    return _git("rev-parse", "--verify", "origin/master").returncode == 0


def tracked_on_master(rel: str) -> bool:
    return _git("cat-file", "-e", f"origin/master:{rel}").returncode == 0


def text_on_master(rel: str) -> str:
    """The file's content on `origin/master`. Raises when git cannot produce it."""
    shown = _git("show", f"origin/master:{rel}")
    if shown.returncode:
        raise RuntimeError(
            f"git show origin/master:{rel} failed: {shown.stderr.strip()}"
        )
    return shown.stdout


def differs_from_master(rel: str) -> bool:
    return _git("diff", "--quiet", "origin/master", "--", rel).returncode != 0


def tracked_python_files() -> list[str]:
    """Every tracked first-party `.py` path, repo-relative.

    `git ls-files` rather than a walk, which from the repo root descends into
    `.claude/worktrees/<name>/` — see test_no_root_anchored_rglob.py for that incident.
    """
    listed = _git("ls-files", "-z", "--", "*.py").stdout
    return [
        rel
        for rel in listed.split("\0")
        if rel and not rel.startswith("ansible/collections/")
    ]


def line_counts() -> dict[str, int]:
    """Repo-relative path -> line count, counting newlines the way `wc -l` does."""
    return {
        rel: (REPO / rel).read_bytes().count(b"\n") for rel in tracked_python_files()
    }


def monkeypatch_counts() -> dict[str, int]:
    """Repo-relative test-module path -> its module-patch count, zeros included.

    Zeros are in the mapping so a listed file whose patches are all gone reads as "remove it"
    rather than as a path that no longer exists.
    """
    tracked = tracked_python_files()
    first_party = first_party_module_names(tracked)
    return {
        rel: count_module_patches((REPO / rel).read_text(errors="replace"), first_party)
        for rel in tracked
        if is_test_file(Path(rel))
    }


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
    with pytest.raises(ValueError):
        parse_allowlist("scripts/a.py 700\nscripts/a.py 800\n")


def test_the_parser_rejects_a_malformed_line():
    with pytest.raises(ValueError):
        parse_allowlist("scripts/a.py\n")


def test_a_tree_within_its_caps_and_its_allowlist_is_clean():
    counts = {"scripts/a.py": 599, "scripts/big.py": 900}
    allow = {"scripts/big.py": 950}
    assert LENGTHS.violations(counts, allow) == []


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


def test_a_raised_entry_is_flagged_even_when_the_guard_changed():
    """The exemptions cover additions only — an existing entry never rises."""
    flagged = raised_entries(
        {"scripts/a.py": 900}, {"scripts/a.py": 901}, "list.txt", guard_changed=True
    )
    assert len(flagged) == 1


def test_a_first_party_name_is_a_module_stem_or_the_directory_holding_one():
    names = first_party_module_names(["scripts/deploy_tools/land_lib/tools.py"])
    assert "tools" in names and "land_lib" in names
    assert "scripts" not in names and "deploy_tools" not in names


def test_a_patch_on_an_imported_first_party_module_is_counted():
    src = "import mod\ndef test_x(monkeypatch):\n    monkeypatch.setattr(mod.sub, 'f', 1)\n"
    assert count_module_patches(src, {"mod"}) == 1


def test_a_patch_on_the_standard_library_is_not_counted():
    """A seam cannot remove one, so counting it would leave an entry stuck above zero."""
    src = "import sys\ndef test_x(monkeypatch):\n    monkeypatch.setattr(sys, 'argv', [])\n"
    assert count_module_patches(src, {"mod"}) == 0


def test_a_patch_on_a_local_object_or_a_string_target_is_not_counted():
    src = (
        "import mod\n"
        "def test_x(monkeypatch, obj):\n"
        "    monkeypatch.setattr(obj, 'f', 1)\n"
        "    monkeypatch.setattr('mod.f', 1)\n"
        "    monkeypatch.setenv('MOD', '1')\n"
    )
    assert count_module_patches(src, {"mod"}) == 0


def test_an_aliased_import_is_still_the_module_it_aliases():
    src = "import a.b as mod\ndef test_x(monkeypatch):\n    monkeypatch.setattr(mod, 'f', 1)\n"
    assert count_module_patches(src, {"b"}) == 1


def test_a_module_bound_by_importlib_is_counted_in_both_spellings():
    """`session-health.py` is not an identifier, so its tests reach it through a spec."""
    spec = "_mod = importlib.util.module_from_spec(_spec)\n"
    named = "_mod = importlib.import_module('scripts.thing')\n"
    patch = "def test_x(monkeypatch):\n    monkeypatch.setattr(_mod, 'f', 1)\n"
    assert count_module_patches(spec + patch, set()) == 1
    assert count_module_patches(named + patch, set()) == 1


# ---------------------------------------------------------------- the live tree


def test_the_census_reaches_every_tree_that_holds_python():
    """Without this, every assertion below passes on a census that has gone quiet."""
    counts = line_counts()
    assert CENSUS_MEMBERS <= set(counts), CENSUS_MEMBERS - set(counts)
    assert sum(1 for rel in counts if cap_for(rel) == TEST_CAP) >= 100
    assert sum(1 for rel in counts if cap_for(rel) == NON_TEST_CAP) >= 100


def test_no_module_is_longer_than_its_cap_or_its_allowlist_entry():
    offenders = LENGTHS.violations(line_counts(), LENGTHS.allowlist())
    assert not offenders, "\n".join(offenders)


def test_no_test_module_patches_more_modules_than_its_allowlist_entry():
    offenders = PATCHES.violations(monkeypatch_counts(), PATCHES.allowlist())
    assert not offenders, "\n".join(offenders)


def test_every_guard_source_exists_at_the_path_named():
    """`differs_from_master` answers False for a path absent on both sides.

    A renamed guard source would therefore read as unchanged, and the exemption that lets a
    widened rule add entries would be off with nothing saying so.
    """
    missing = [rel for rel in GUARD_SOURCES if not (REPO / rel).is_file()]
    assert not missing, f"GUARD_SOURCES names paths that do not exist: {missing}"


def test_the_master_read_returns_content_for_a_path_master_tracks():
    """The comparison below skips until the lists reach master; this keeps the read proved."""
    if not master_is_fetched():
        pytest.skip("origin/master is not fetched in this checkout")
    assert tracked_on_master("ansible/tests/_helpers.py")
    assert text_on_master("ansible/tests/_helpers.py").startswith('"""')
    assert not tracked_on_master("ansible/tests/no_such_file.py")


def test_the_master_read_raises_rather_than_returning_empty_for_an_unknown_path():
    """A failed read must fail the comparison, not quietly look like an empty allowlist."""
    if not master_is_fetched():
        pytest.skip("origin/master is not fetched in this checkout")
    with pytest.raises(RuntimeError):
        text_on_master("ansible/tests/no_such_file.py")


@pytest.mark.parametrize("ratchet", [LENGTHS, PATCHES], ids=lambda r: r.path.name)
def test_no_allowlist_entry_rose_against_origin_master(ratchet: Ratchet):
    """A commit's own counts cannot see a diff that grows a file and its entry together."""
    rel = ratchet.path.relative_to(REPO).as_posix()
    if not master_is_fetched():
        pytest.skip(
            "origin/master is not fetched — a shallow CI checkout. `prek run --all-files` "
            "runs this locally, where the ref exists."
        )
    if not tracked_on_master(rel):
        pytest.skip(
            f"{rel} is not on origin/master yet, so there is nothing to compare"
        )
    new = ratchet.allowlist()
    offenders = raised_entries(
        parse_allowlist(text_on_master(rel)),
        new,
        ratchet.path.name,
        untracked_on_master=[p for p in new if not tracked_on_master(p)],
        guard_changed=any(differs_from_master(p) for p in GUARD_SOURCES),
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
