"""The two allowlist ratchets: the caps, the parser and the comparisons.

The monkeypatch counter is `ansible/tests/_ratchet_patches.py`. Everything here is a function over mappings and strings, with one exception:
`Ratchet.allowlist()` reads the list file the dataclass points at. The census of the tree is
`ansible/tests/_ratchet_census.py` and the reads of the merge base are in
`ansible/tests/repo/test_module_length_ratchet.py`, which also holds the tests for all three.
The counter's tests are `ansible/tests/repo/test_ratchet_rules.py`.

Module length and `monkeypatch` on a first-party module are ratcheted the same way. A cap says
what a new file may do, an allowlist records what the files that already exceed it do today,
and an entry may only fall or disappear. A file that grows past its entry fails, and so does
one that drops back under the cap while keeping its line — the list is a record of remaining
work, so finishing the work deletes the line.

The caps are 600 lines for a module and 500 for a test module (`wc -l` semantics: the number
of newline characters). Test modules get the tighter number because a test file grows by
repetition rather than by branching, so length there buys much less than in a module the rest
of the tree calls. The monkeypatch cap is 0: a patch on a first-party module pins that
module's name into a test, so a module carrying any has not got a seam yet, and
`scripts/deploy_tools/land_lib/tools.py` with `scripts/deploy_tools/tests/_land_fakes.py` is
the shape that replaces one.

Two things enforce "only falls". A count over its own entry fails (`Ratchet.violations`).
Beyond that, `raised_entries` diffs the lists as the working tree has them against their
merge base with `origin/master`, or a branch could grow a file and raise its own line in the
same diff. An added path fails there — a split that produced another oversized module has not
finished — and so does a raised entry, with three exemptions, all passed in as plain values by
the caller that reads git:

- A module-length entry ending `# conjoined: <why>` may be added, and rises only in a diff
  that rewrites its reason to name the new max. The `# DECIDED:` marker in `module_length_allowlist.txt` says why.
- A path the merge base does not track may be added. That is a new or renamed file, and a
  rename would otherwise read as a deletion plus a forbidden addition.
- A changed guard lets any path be added AND lets an entry rise. Widening the heuristic (as
  the `importlib` fix did) finds patches that were always there, in files that already have an
  entry as well as in files that do not, so both moves have to be possible in the branch that
  widens. The trigger is four files and one function: the whole of `_ratchet.py`, the whole
  of `_ratchet_patches.py`, the whole of `_ratchet_census.py` — a widened census finds files
  that were always over, the same way a widened heuristic does — the whole of the test
  module, and only the text of `_helpers.is_test_file`. The census module is the loosest, because a comment
  edit there also exempts every raise; it is in the set because nothing else records what the
  lists are allowed to contain, and it is 121 lines nobody edits in passing. `_helpers.py` is
  the counter-example that fixes the width: 198 modules import it, so comparing all of it
  would wave through a change that has nothing to do with the guard.

What the monkeypatch heuristic counts, and what it misses, is in `_ratchet_patches.py`, beside
the counter.

# DECIDED: an entry has to MATCH its file, not merely bound it. This module said the
opposite until 2026-09-05 -- a listed file could sit anywhere between its cap and its listed
max, so that a one-line deletion in a 900-line module would not force an allowlist edit. The
gap that buys is regrowth headroom no check reports: three entries drifted during the
module-split plan (PRs #1108-#1191) and every one was caught by a reviewer counting by hand.
`Ratchet.violations` now flags `count < listed` too, naming the number to write. The cost is
the one the old stance avoided -- a PR that shrinks a listed file edits its line -- and that is
one line, in a sorted file, in the same PR that moved the number. Two PRs colliding there are
two PRs already colliding in the file itself. Re-examined on 2026-09-28 (#2809) and kept: 70
commits touched a list in the 30 days to that date, 49 of them only lowering or deleting an
entry, and bound semantics would have left every one of those 49 a silent regrowth headroom.
What changed instead is that nobody writes the edit by hand -- `tighten` below is the fixer,
`scripts/dev/tighten_ratchets.py --tighten` is the writer that runs it, and the
`tighten-ratchet-allowlists` prek hook runs that on every commit touching Python.

The census that feeds these functions is `ansible/tests/_ratchet_census.py`, and the tests
for both are in `ansible/tests/repo/test_module_length_ratchet.py`.
"""

import ast
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from pathlib import Path

from _helpers import is_test_file

NON_TEST_CAP = 600
TEST_CAP = 500


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


# `<path> <max>  # conjoined: <why a split piece could not be read without its parent>`.
CONJOINED = "conjoined:"


def conjoined_reasons(text: str) -> dict[str, str]:
    """path -> the non-empty `conjoined:` reason on its entry's own line, which
    `parse_allowlist` drops."""
    found: dict[str, str] = {}
    for raw in text.splitlines():
        line, _, comment = raw.partition("#")
        match line.split():
            case [path, number] if number.isdigit():
                keyword, _, reason = comment.strip().partition(" ")
                if keyword == CONJOINED and reason.strip():
                    found[path] = reason.strip()
    return found


def raised_entries(
    old: Mapping[str, int],
    new: Mapping[str, int],
    name: str,
    *,
    untracked_on_master: Collection[str] = (),
    guard_changed: bool = False,
    conjoined: Mapping[str, str] | None = None,
    old_conjoined: Mapping[str, str] | None = None,
) -> list[str]:
    """Every way `new` is a looser allowlist than `old`, as sentences naming the fix.

    Args:
        old: the list as the merge base with `origin/master` has it.
        new: the list as the working tree has it.
        name: the allowlist's filename, for the message.
        untracked_on_master: paths the merge base does not track, which may be added.
        guard_changed: whether the guard differs from the merge base, which may add any path
            and may raise any entry.
        conjoined: path -> the `conjoined:` reason in `new`, which may add the path, and may
            raise it when the reason differs from `old_conjoined`, the base's reasons, and
            names the new max as a word.
    """
    conjoined = conjoined or {}
    old_conjoined = old_conjoined or {}
    found = []
    for path, limit in sorted(new.items()):
        if path in old:
            # DECIDED: a changed guard may RAISE an entry, not only add one. A widening finds
            # patches that were always there in files that already have a line, which is what
            # the string-target fix hit: test_session_health.py went 33 -> 36 and no exemption
            # covered it. The cost is that `raised_entries` serves both ratchets, so a branch
            # editing _ratchet.py may also raise a module-length entry; the bound is that the
            # trigger is narrow (this file, its test module, or _helpers.is_test_file) and the
            # guard diff is in the same PR a reviewer reads.
            # A rewording alone would wave through any growth, so the restated reason has
            # to name the number it approves (#4301 review).
            reason = conjoined.get(path)
            restated = (
                reason is not None
                and reason != old_conjoined.get(path)
                and str(limit) in reason.replace(",", " ").split()
            )
            if limit > old[path] and not guard_changed and not restated:
                found.append(
                    f"{path}: {name} says {limit}, up from {old[path]} on the merge base "
                    f"with origin/master. "
                    f"An entry only ever falls: lower the file, not the bar."
                )
        elif (
            not guard_changed
            and path not in untracked_on_master
            and path not in conjoined
        ):
            found.append(
                f"{path}: added to {name} at {limit}, though the merge base already tracks "
                f"the file and the guard is unchanged against it. A file that was "
                f"already there has to meet the cap — split it, or state why a split would "
                f"be conjoined: `# {CONJOINED} <why>`."
            )
    return found


def function_source(text: str, name: str) -> str | None:
    """The source of the top-level function `name` in `text`, or None when it has none."""
    for node in ast.parse(text).body:
        if (
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name == name
        ):
            return ast.get_source_segment(text, node)
    return None


def function_differs(old_text: str, new_text: str, name: str) -> bool:
    """Whether one function's source differs between two versions of a file.

    Comparing the whole of `_helpers.py` would make any edit to it — 198 modules import it —
    stand in for a change to the rule that decides a path's cap.
    """
    return function_source(old_text, name) != function_source(new_text, name)


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
            # DECIDED: an entry has to MATCH its file, not merely bound it. The reasoning,
            # and the stance this reversed, are in this module's docstring.
            elif count < listed:
                found.append(
                    f"{rel}: {count} {self.unit}, under its allowlisted max of {listed} — "
                    f"lower that line in {name} to {count}. An entry records what the file "
                    f"is today; the gap is regrowth headroom nothing would report."
                )
        for rel in sorted(set(allow) - set(counts)):
            found.append(
                f"{rel}: listed in {name} at {allow[rel]} {self.unit}, but no tracked file "
                f"has that path — delete the line, or fix the path if the file moved."
            )
        return found


def tighten(text: str, counts: Mapping[str, int], cap_of: Callable[[str], int]) -> str:
    """`text` with every entry lowered to what its file is today, leaving the rest verbatim.

    The three edits a shrink needs, and nothing else:

    - an entry over its file's count falls to that count;
    - an entry for a path no tracked file has is deleted;
    - an entry for a file back at or under its cap is deleted, because the list records
      remaining work only.

    An entry at or UNDER its file's count is left exactly as written, so a file that grew
    still fails `Ratchet.violations` — a fixer that raised a bar would be the ratchet's
    opposite. Comments, blank lines and anything this grammar does not recognise pass
    through unchanged, which keeps the header block and the sort order intact.
    """
    kept: list[str] = []
    for raw in text.splitlines():
        match raw.split("#", 1)[0].strip().split():
            case [path, number] if number.isdigit():
                count = counts.get(path)
                if count is None or count <= cap_of(path):
                    continue
                # Rewriting the number inside the line, rather than reconstructing the line,
                # is what leaves a trailing comment and its spacing alone.
                kept.append(
                    raw
                    if count >= int(number)
                    else raw.replace(f"{path} {number}", f"{path} {count}", 1)
                )
            case _:
                kept.append(raw)
    return "".join(f"{line}\n" for line in kept)
