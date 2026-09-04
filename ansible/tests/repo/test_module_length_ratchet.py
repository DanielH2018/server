"""Two ratchets over the Python tree: module length, and monkeypatch on a first-party module.

Both work the same way. A cap says what a new file may do, an allowlist records what the
files that already exceed it do today, and an entry may only fall or disappear. A file that
grows past its entry fails, and so does one that drops back under the cap while keeping its
line — the list is a record of remaining work, so finishing the work deletes the line.

The caps are 600 lines for a module and 500 for a test module (`wc -l` semantics: the number
of newline characters). The monkeypatch cap is 0: a patch on a first-party module pins that
module's name into a test, so a module carrying any has not got a seam yet, and
`scripts/deploy_tools/land_lib/tools.py` with `scripts/deploy_tools/tests/_land_fakes.py` is
the shape that replaces one.

The allowlists are `module_length_allowlist.txt` and `monkeypatch_allowlist.txt` beside this
file: one `<repo-relative-path> <max>` line per file, sorted, `#` comments allowed. One line
per file so the split PRs that each lower one entry merge cleanly.

Two things enforce "only falls". Within a commit, a count over its own entry fails. Across
commits, `test_no_allowlist_entry_rose_against_origin_master` diffs the lists against
`git show origin/master:<path>`, or a PR could grow a file and raise its own line in the same
diff. It skips, saying so, where that ref is absent (a shallow CI checkout); `prek` runs it
locally, where the ref exists, and that is the gate it is written for. An added path fails
even when the new file is over a cap: a split that produced another oversized module has not
finished, and the number to change is the file's length.

What the monkeypatch heuristic counts, and what it misses:

- Counted: `monkeypatch.setattr(<name>, ...)` or `...(<a>.<b>, ...)` where the root name is
  bound to a FIRST-PARTY module — one whose name matches a tracked `.py` file or the
  directory that directly holds one. `sys`, `subprocess` and `urllib` are not counted: no
  seam can remove a patch on the standard library, so those entries could never reach zero.
- Counted: a name assigned from `importlib.util.module_from_spec(...)` or
  `importlib.import_module(...)`. That is how the hook and cluster-side tests reach a module
  whose filename is not an identifier (`session-health.py`); 57 patches across five files
  were invisible while only `import` statements were read.
- Not counted: the string-target form `monkeypatch.setattr("mod.attr", ...)`, a receiver
  spelled anything but `monkeypatch`, and `delattr`/`setitem`/`setenv`/`chdir`.
- Not counted: a patch on an imported class or function, unless its name happens to match a
  first-party module name. Restricting roots to module names is what keeps the standard
  library out, and an import statement does not say which kind of object it binds.
- Over-counted: `import_module("json")` would count, because the argument is not resolved.
  No test in this tree does that.
- Per file, so moving patches from a listed test module into a new one lowers one entry and
  adds another while removing no patching. That is inherent in the one-line-per-file format;
  a `# TOTAL n` line would conflict on every parallel PR. The added entry shows in the diff.

A listed file may sit anywhere between its cap and its listed max without failing. That is
deliberate: otherwise every one-line deletion in a 900-line module would force an allowlist
edit, and parallel PRs would collide on lines they had no reason to touch.

Run: uv run pytest ansible/tests/repo/test_module_length_ratchet.py
"""

import ast
import subprocess
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

from _helpers import REPO, is_test_file

NON_TEST_CAP = 600
TEST_CAP = 500

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

# `mod = importlib.util.module_from_spec(spec)` and `mod = importlib.import_module("x")` both
# bind a module to a plain name, which no import statement records.
DYNAMIC_IMPORTS = frozenset({"module_from_spec", "import_module"})


def cap_for(rel: str) -> int:
    """The line cap a repo-relative path has to meet."""
    return TEST_CAP if is_test_file(Path(rel)) else NON_TEST_CAP


def parse_allowlist(text: str) -> dict[str, int]:
    """`<path> <max>` lines, in file order, rejecting a duplicate or a malformed line.

    A duplicate is the merge artifact this format invites: two PRs adding the same path with
    different maxima, where last-wins would silently RAISE one of them.
    """
    parsed: dict[str, int] = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        match line.split():
            case [path, number] if number.isdigit():
                if path in parsed:
                    raise ValueError(f"line {lineno}: {path} is listed twice")
                parsed[path] = int(number)
            case _:
                raise ValueError(f"line {lineno}: expected `<path> <max>`, got {raw!r}")
    return parsed


def raised_entries(
    old: Mapping[str, int], new: Mapping[str, int], name: str
) -> list[str]:
    """Every way `new` is a looser allowlist than `old`, as sentences naming the fix."""
    found = []
    for path, limit in sorted(new.items()):
        if path not in old:
            found.append(
                f"{path}: added to {name} at {limit}. A file this list has never carried "
                f"has to meet the cap on its own — split it further rather than listing it."
            )
        elif limit > old[path]:
            found.append(
                f"{path}: {name} says {limit}, up from {old[path]} on origin/master. "
                f"An entry only ever falls: lower the file, not the bar."
            )
    return found


@dataclass(frozen=True)
class Ratchet:
    """One allowlist, its cap policy and the message it fails with."""

    path: Path
    unit: str
    remedy: str
    cap_of: Callable[[str], int]

    def allowlist(self) -> dict[str, int]:
        return parse_allowlist(self.path.read_text())

    def allowlist_on_master(self) -> dict[str, int] | None:
        """The same list as `origin/master` has it, or None when that ref is unavailable."""
        rel = self.path.relative_to(REPO).as_posix()
        shown = subprocess.run(
            ["git", "show", f"origin/master:{rel}"],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=False,
        )
        return None if shown.returncode else parse_allowlist(shown.stdout)

    def violations(
        self, counts: Mapping[str, int], allow: Mapping[str, int]
    ) -> list[str]:
        """Every way `counts` disagrees with `allow`, as sentences naming the fix."""
        name = self.path.name
        found = []
        for rel, count in sorted(counts.items()):
            cap, listed = self.cap_of(rel), allow.get(rel)
            if listed is None:
                if count > cap:
                    found.append(
                        f"{rel}: {count} {self.unit}, over the cap of {cap}. {self.remedy} "
                        f"If it is a file an earlier slice was meant to shrink, its line went "
                        f"missing from {name} — restore it rather than adding a new one."
                    )
            elif count > listed:
                found.append(
                    f"{rel}: {count} {self.unit}, over its allowlisted max of {listed}. "
                    f"{name} only ever falls: lower the file, not the bar."
                )
            elif count <= cap:
                found.append(
                    f"{rel}: {count} {self.unit}, at or under the cap of {cap} — remove it "
                    f"from {name}. The list records remaining work only."
                )
        for rel in sorted(set(allow) - set(counts)):
            found.append(
                f"{rel}: listed in {name} at {allow[rel]} {self.unit}, but no tracked file "
                f"has that path — delete the line, or fix the path if the file moved."
            )
        return found


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


def tracked_python_files() -> list[str]:
    """Every tracked first-party `.py` path, repo-relative.

    `git ls-files` rather than a walk, which from the repo root descends into
    `.claude/worktrees/<name>/` — see test_no_root_anchored_rglob.py for that incident.
    """
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.py"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
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


def first_party_module_names(tracked: Iterable[str]) -> frozenset[str]:
    """Every bare name an `import` in this repo could bind to a module of this repo.

    A module's own stem, plus the directory that directly holds it — that directory is what a
    dotted `bridge.config` import names. Directories further up (`ansible`, `roles`) are left
    out: nothing imports them, and `ansible` is a third-party package.
    """
    names: set[str] = set()
    for rel in tracked:
        *parents, name = rel.split("/")
        names.add(name.removesuffix(".py"))
        if parents:
            names.add(parents[-1])
    return frozenset(names)


def _bound_module_names(tree: ast.AST, first_party: Collection[str]) -> set[str]:
    """The local names in `tree` that hold a first-party module."""
    bound: set[str] = set()
    for node in ast.walk(tree):
        match node:
            case ast.Import(names=aliases):
                for alias in aliases:
                    head, tail = alias.name.split(".")[0], alias.name.split(".")[-1]
                    # `import a.b` binds `a`; `import a.b as m` binds the module `a.b`.
                    local, target = (
                        (alias.asname, tail) if alias.asname else (head, head)
                    )
                    if target in first_party:
                        bound.add(local)
            case ast.ImportFrom(names=aliases):
                bound |= {
                    alias.asname or alias.name
                    for alias in aliases
                    if alias.name in first_party
                }
            case ast.Assign(
                targets=targets, value=ast.Call(func=ast.Attribute(attr=attr))
            ) if attr in DYNAMIC_IMPORTS:
                bound |= {t.id for t in targets if isinstance(t, ast.Name)}
    return bound


def count_module_patches(source: str, first_party: Collection[str]) -> int:
    """How many `monkeypatch.setattr` calls in `source` target a first-party module.

    The module docstring lists what this deliberately does not see.
    """
    tree = ast.parse(source)
    bound = _bound_module_names(tree, first_party)

    counted = 0
    for node in ast.walk(tree):
        match node:
            case ast.Call(
                func=ast.Attribute(value=ast.Name(id="monkeypatch"), attr="setattr"),
                args=[target, *_],
            ):
                while isinstance(target, ast.Attribute):
                    target = target.value
                if isinstance(target, ast.Name) and target.id in bound:
                    counted += 1
    return counted


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
    new = {"scripts/a.py": 800}
    assert raised_entries(old, new, "list.txt") == []


def test_a_raised_entry_is_flagged():
    flagged = raised_entries({"scripts/a.py": 900}, {"scripts/a.py": 901}, "list.txt")
    assert len(flagged) == 1
    assert "up from 900" in flagged[0]


def test_a_newly_added_path_is_flagged():
    flagged = raised_entries({}, {"scripts/new.py": 900}, "list.txt")
    assert len(flagged) == 1
    assert "added to list.txt" in flagged[0]


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


@pytest.mark.parametrize("ratchet", [LENGTHS, PATCHES], ids=lambda r: r.path.name)
def test_no_allowlist_entry_rose_against_origin_master(ratchet: Ratchet):
    """A commit's own counts cannot see a diff that grows a file and its entry together."""
    on_master = ratchet.allowlist_on_master()
    if on_master is None:
        pytest.skip(
            f"origin/master:{ratchet.path.name} is unreadable — either the ref is not "
            f"fetched (a shallow CI checkout) or the list is new. `prek run --all-files` "
            f"runs this locally, where the ref exists."
        )
    offenders = raised_entries(on_master, ratchet.allowlist(), ratchet.path.name)
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
