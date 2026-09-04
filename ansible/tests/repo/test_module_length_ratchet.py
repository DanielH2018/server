"""Two ratchets over the Python tree: module length, and monkeypatch on a production module.

Both work the same way. A cap says what a new file may do, an allowlist records what the
files that already exceed it do today, and an entry may only fall or disappear. A file that
grows past its entry fails, and so does one that drops back under the cap while keeping its
line — the list is a record of remaining work, so finishing the work deletes the line.

The caps are 600 lines for a module and 500 for a test module (`wc -l` semantics: the number
of newline characters). Test modules are longer per idea and shorter-lived per line, which is
why they get the tighter number rather than the looser one. The monkeypatch cap is 0: a patch
on a production module pins that module's name into a test, so a module carrying any has not
got a seam yet, and `scripts/deploy_tools/land_lib/tools.py` with
`scripts/deploy_tools/tests/_land_fakes.py` is the shape that replaces one.

The allowlists are `module_length_allowlist.txt` and `monkeypatch_allowlist.txt` beside this
file: one `<repo-relative-path> <max>` line per file, sorted, `#` comments allowed. One line
per file so the 23 module-split PRs that each lower one entry merge cleanly.

What the monkeypatch heuristic counts, and what it misses:

- Counted: `monkeypatch.setattr(<name>, ...)` and `monkeypatch.setattr(<a>.<b>, ...)` where the
  root name is bound by an `import` or `from ... import` in the same file.
- Not counted: the string-target form `monkeypatch.setattr("mod.attr", ...)`, a receiver
  spelled anything but `monkeypatch`, and `delattr`/`setitem`/`setenv`/`chdir`.
- `from x import y` may bind a class rather than a module, so a patch on an imported class
  counts too. That is the intended reading — it pins a production name either way.
- The count is per file, so moving patches from a listed test module into a new one lowers one
  entry and adds another without removing any patching. That is inherent in the one-line-per-
  file format, and a total ceiling is not the fix: a `# TOTAL n` line would conflict on every
  parallel PR. Reviewers see the added entry in the diff.

A listed file may sit anywhere between its cap and its listed max without failing. That is
deliberate: otherwise every one-line deletion in a 900-line module would force an allowlist
edit, and 23 PRs would collide on lines they had no reason to touch.

Run: uv run pytest ansible/tests/repo/test_module_length_ratchet.py
"""

import ast
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

from _helpers import REPO

NON_TEST_CAP = 600
TEST_CAP = 500

HERE = REPO / "ansible" / "tests" / "repo"


def is_test_path(rel: str) -> bool:
    """Whether a repo-relative path is a test module, by basename or by directory."""
    *parents, name = rel.split("/")
    return name.startswith("test_") or name == "conftest.py" or "tests" in parents


def cap_for(rel: str) -> int:
    """The line cap a repo-relative path has to meet."""
    return TEST_CAP if is_test_path(rel) else NON_TEST_CAP


def parse_allowlist(text: str) -> dict[str, int]:
    """`<path> <max>` lines, in file order, rejecting a duplicate or a malformed line.

    A duplicate is the merge artifact this format invites: two PRs adding the same path with
    different maxima. Last-wins would silently RAISE one of them, which is the one thing the
    ratchet exists to prevent, so it raises instead.
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


@dataclass(frozen=True)
class Ratchet:
    """One allowlist, its cap policy and the message it fails with."""

    path: Path
    unit: str
    remedy: str
    cap_of: Callable[[str], int]

    def allowlist(self) -> dict[str, int]:
        return parse_allowlist(self.path.read_text())

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
    unit="monkeypatch.setattr calls on an imported module",
    remedy=(
        "Give the module under test a seam instead — a frozen dataclass of injectable "
        "boundaries, as in scripts/deploy_tools/land_lib/tools.py with its fakes in "
        "scripts/deploy_tools/tests/_land_fakes.py."
    ),
    cap_of=lambda rel: 0,
)


def tracked_python_files() -> list[str]:
    """Every tracked first-party `.py` path, repo-relative.

    `git ls-files` rather than a filesystem walk: a walk from the repo root descends into
    `.claude/worktrees/<name>/`, judging this commit against another session's older copies
    (see test_no_root_anchored_rglob.py for the incident).
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


def count_module_patches(source: str) -> int:
    """How many `monkeypatch.setattr` calls in `source` target an imported name.

    The module docstring lists what this deliberately does not see.
    """
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.asname or a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.update(a.asname or a.name for a in node.names)

    counted = 0
    for node in ast.walk(tree):
        match node:
            case ast.Call(
                func=ast.Attribute(value=ast.Name(id="monkeypatch"), attr="setattr"),
                args=[target, *_],
            ):
                while isinstance(target, ast.Attribute):
                    target = target.value
                if isinstance(target, ast.Name) and target.id in imported:
                    counted += 1
    return counted


def monkeypatch_counts() -> dict[str, int]:
    """Repo-relative test-module path -> its module-patch count, zeros included.

    Zeros are in the mapping on purpose: that is what lets a listed file whose patches are all
    gone be reported as "remove it" rather than as a path that no longer exists.
    """
    return {
        rel: count_module_patches((REPO / rel).read_text(errors="replace"))
        for rel in tracked_python_files()
        if is_test_path(rel)
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


def test_a_patch_on_an_imported_module_is_counted():
    src = "import mod\ndef test_x(monkeypatch):\n    monkeypatch.setattr(mod.sub, 'f', 1)\n"
    assert count_module_patches(src) == 1


def test_a_patch_on_a_local_object_or_a_string_target_is_not_counted():
    src = (
        "import mod\n"
        "def test_x(monkeypatch, obj):\n"
        "    monkeypatch.setattr(obj, 'f', 1)\n"
        "    monkeypatch.setattr('mod.f', 1)\n"
        "    monkeypatch.setenv('MOD', '1')\n"
    )
    assert count_module_patches(src) == 0


def test_an_aliased_import_is_still_the_module_it_aliases():
    src = "import a.b as mod\ndef test_x(monkeypatch):\n    monkeypatch.setattr(mod, 'f', 1)\n"
    assert count_module_patches(src) == 1


# ---------------------------------------------------------------- the live tree


def test_the_census_reaches_the_whole_tree_and_exercises_both_caps():
    """Without this, every assertion below passes on an empty census."""
    counts = line_counts()
    assert len(counts) >= 400, len(counts)
    assert sum(1 for rel in counts if cap_for(rel) == TEST_CAP) >= 100
    assert sum(1 for rel in counts if cap_for(rel) == NON_TEST_CAP) >= 100


def test_no_module_is_longer_than_its_cap_or_its_allowlist_entry():
    offenders = LENGTHS.violations(line_counts(), LENGTHS.allowlist())
    assert not offenders, "\n".join(offenders)


def test_no_test_module_patches_more_modules_than_its_allowlist_entry():
    offenders = PATCHES.violations(monkeypatch_counts(), PATCHES.allowlist())
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
    """Sorted, one line per file, is what makes 23 parallel PRs merge cleanly."""
    for ratchet in (LENGTHS, PATCHES):
        listed = list(ratchet.allowlist())
        assert listed == sorted(listed), ratchet.path.name
