"""The two allowlist ratchets, as pure functions over mappings.

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

Two things enforce "only falls". Within a commit, a count over its own entry fails
(`Ratchet.violations`). Across commits, `raised_entries` diffs the committed lists against
`origin/master`, or a PR could grow a file and raise its own line in the same diff. An added
path fails there — a split that produced another oversized module has not finished — with two
exemptions, both passed in as plain values by the caller that reads git:

- A path `origin/master` does not track. That is a new or renamed file, and a rename would
  otherwise read as a deletion plus a forbidden addition.
- A changed guard. Widening the heuristic (as the `importlib` fix did) finds patches that were
  always there, and those files have to be able to enter the list in the same PR.

What the monkeypatch heuristic counts, and what it misses:

- Counted: `monkeypatch.setattr(<name>, ...)` or `...(<a>.<b>, ...)` where the root name is
  bound to a FIRST-PARTY module — one whose name matches a tracked `.py` file or the
  directory that directly holds one. `sys`, `subprocess` and `urllib` are not counted: no
  seam can remove a patch on the standard library, so those entries could never reach zero.
- Counted: a name assigned from `importlib.util.module_from_spec(...)` or
  `importlib.import_module(...)`. That is how the hook and cluster-side tests reach a module
  whose filename is not an identifier (`session-health.py`); 57 patches across five files
  were invisible while only `import` statements were read.
- Not counted: a module handed to the test by a fixture. `gitops_deploy/tests/conftest.py`
  returns the module itself, so `test_gitops_deploy_fetch_skip.py`,
  `test_gitops_deploy_alert_delivery.py`, `test_gitops_deploy_main_branches.py` and
  `test_staging_tick_ledger.py` all count 0 while patching a production module through the
  fixture argument. Resolving that means following a fixture's return type across files.
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

The census that feeds these functions, and the tests for them, are in
`ansible/tests/repo/test_module_length_ratchet.py`.
"""

import ast
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from _helpers import is_test_file

NON_TEST_CAP = 600
TEST_CAP = 500

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
    old: Mapping[str, int],
    new: Mapping[str, int],
    name: str,
    *,
    untracked_on_master: Collection[str] = (),
    guard_changed: bool = False,
) -> list[str]:
    """Every way `new` is a looser allowlist than `old`, as sentences naming the fix.

    Args:
        old: the list as `origin/master` has it.
        new: the list as this commit has it.
        name: the allowlist's filename, for the message.
        untracked_on_master: paths `origin/master` does not track, which may be added.
        guard_changed: whether this commit changes the guard, which may add any path.
    """
    found = []
    for path, limit in sorted(new.items()):
        if path in old:
            if limit > old[path]:
                found.append(
                    f"{path}: {name} says {limit}, up from {old[path]} on origin/master. "
                    f"An entry only ever falls: lower the file, not the bar."
                )
        elif not guard_changed and path not in untracked_on_master:
            found.append(
                f"{path}: added to {name} at {limit}, though origin/master already tracks "
                f"the file and this commit does not change the guard. A file that was "
                f"already there has to meet the cap — split it rather than listing it."
            )
    return found


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
