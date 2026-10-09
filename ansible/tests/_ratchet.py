"""The two allowlist ratchets: the caps, the comparisons and the monkeypatch counter.

Everything here is a function over mappings and strings, with one exception:
`Ratchet.allowlist()` reads the list file the dataclass points at. The census of the tree is
`ansible/tests/_ratchet_census.py` and the reads of the merge base are in
`ansible/tests/repo/test_module_length_ratchet.py`, which also holds the tests for all three.

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
finished — and so does a raised entry, with two exemptions, both passed in as plain values by
the caller that reads git:

- A path the merge base does not track may be added. That is a new or renamed file, and a
  rename would otherwise read as a deletion plus a forbidden addition.
- A changed guard lets any path be added AND lets an entry rise. Widening the heuristic (as
  the `importlib` fix did) finds patches that were always there, in files that already have an
  entry as well as in files that do not, so both moves have to be possible in the branch that
  widens. The trigger is three files and one function: the whole of `_ratchet.py`, the whole
  of `_ratchet_census.py` — a widened census finds files that were always over, the same way
  a widened heuristic does — the whole of the test module, and only the text of
  `_helpers.is_test_file`. The census module is the loosest of the three, because a comment
  edit there also exempts every raise; it is in the set because nothing else records what the
  lists are allowed to contain, and it is 121 lines nobody edits in passing. `_helpers.py` is
  the counter-example that fixes the width: 198 modules import it, so comparing all of it
  would wave through a change that has nothing to do with the guard.

What the monkeypatch heuristic counts, and what it misses:

- Counted: `monkeypatch.setattr(<name>, ...)` or `...(<a>.<b>, ...)` where the root name is
  bound to a FIRST-PARTY module — one whose name matches a tracked `.py` file or the
  directory that directly holds one. `sys`, `subprocess` and `urllib` are not counted: no
  seam can remove a patch on the standard library, so those entries could never reach zero.
- Counted: a name assigned from `importlib.util.module_from_spec(...)` or
  `importlib.import_module(...)`. That is how the hook and cluster-side tests reach a module
  whose filename is not an identifier (`session-health.py`); 57 patches across five files
  were invisible while only `import` statements were read.
- Counted: a parameter named after a conftest fixture that hands back a module. A fixture is
  read as module-returning when it is annotated `-> ModuleType`, or when it imports a name and
  returns it. `gitops_deploy/tests/conftest.py` does both, and three of its sibling test
  modules patched the deployer through that argument while counting 0 — five patches in
  `test_gitops_deploy_alert_channels.py`, four and two in the other two. The caller collects the fixture names from
  every `conftest.py` on a test's directory chain, the way pytest resolves one.
- Not counted: a fixture that returns a module with neither the annotation nor a bare
  `import`/`return` of the same name — a factory closure, say. The annotation is the signal
  the repo already writes; a dataflow analysis across a conftest's own imports is not.
- Counted: the string-target form `monkeypatch.setattr("<mod>.<attr>", ...)`, when the first
  dotted segment is a first-party module name. It is the object form's equal at runtime and
  pins the same module name into the test, so counting one and not the other selected a
  spelling rather than a design: `scripts/dev/tests/test_prune_worktrees.py` patched
  first-party internals fourteen times and measured 0, and on 2026-09-05 an implementer chose
  the string form there because it was the form the ratchet did not see. The string carries
  the module's own dotted name rather than a local alias, so its root is matched against the
  first-party names directly and not against what the test imported. `"subprocess.run"` stays
  out for the same reason a patch on `sys` does.
- Counted: an assignment whose target is an attribute chain rooted at a first-party module
  name, `mod.attr = double` or `mod.sub.attr = double`, plain, augmented, annotated or inside a
  tuple target. It does what `monkeypatch.setattr` does and pins the same name into the test,
  with less safety: an exception between the save and the `try` leaks the double into later
  tests (#3670). Four such patches were hiding behind ty suppressions.
  An assignment inside a `finally:` block is not counted, because that is the restore half of
  the same patch. The root is resolved the way the object form of `monkeypatch.setattr` is,
  so `cfg.X = 1` on a local object and `sys.argv = []` on the standard library stay out.
- Not counted: a receiver spelled anything but `monkeypatch`, a `setattr(mod, "x", v)` call,
  and `delattr`/`setitem`/`setenv`/`chdir`. A restore written outside a `finally:` block
  counts as a second patch.
- Not counted: a patch on an imported class or function, unless its name happens to match a
  first-party module name. Restricting roots to module names is what keeps the standard
  library out, and an import statement does not say which kind of object it binds.
- Over-counted: `import_module("json")` would count, because the argument is not resolved.
  No test in this tree does that.
- Per file, so moving patches from a listed test module into a new one lowers one entry and
  adds another while removing no patching. That is inherent in the one-line-per-file format;
  a `# TOTAL n` line would conflict on every parallel PR. The added entry shows in the diff.

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
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from _helpers import is_test_file

NON_TEST_CAP = 600
TEST_CAP = 500

# `mod = importlib.util.module_from_spec(spec)` and `mod = importlib.import_module("x")` both
# bind a module to a plain name, which no import statement records.
DYNAMIC_IMPORTS = frozenset({"module_from_spec", "import_module"})

# What a conftest fixture writes when it hands a test the module itself.
MODULE_ANNOTATIONS = frozenset({"ModuleType", "types.ModuleType"})


def _returns_a_module(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether this function hands back a module, by its annotation or by its body.

    Two spellings, because a conftest writes either: `-> ModuleType`, or an `import x` inside
    the fixture followed by `return x` (which is how a fixture defers an import that must not
    run at collection).
    """
    if fn.returns is not None and ast.unparse(fn.returns) in MODULE_ANNOTATIONS:
        return True
    imported = {
        alias.asname or alias.name.split(".")[0]
        for node in ast.walk(fn)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    return any(
        isinstance(node, ast.Return)
        and isinstance(node.value, ast.Name)
        and node.value.id in imported
        for node in ast.walk(fn)
    )


def module_fixture_names(source: str) -> frozenset[str]:
    """Every top-level `@pytest.fixture` in `source` that hands back a module.

    A test module never imports these — it names one as a parameter and pytest passes the
    module in, so `_bound_module_names` has no import statement to read. The caller unions
    this over every `conftest.py` on a test's directory chain, the way pytest resolves one.
    """
    return frozenset(
        node.name
        for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and any(
            ast.unparse(dec).split("(")[0].endswith("fixture")
            for dec in node.decorator_list
        )
        and _returns_a_module(node)
    )


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
        old: the list as the merge base with `origin/master` has it.
        new: the list as the working tree has it.
        name: the allowlist's filename, for the message.
        untracked_on_master: paths the merge base does not track, which may be added.
        guard_changed: whether the guard differs from the merge base, which may add any path
            and may raise any entry.
    """
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
            if limit > old[path] and not guard_changed:
                found.append(
                    f"{path}: {name} says {limit}, up from {old[path]} on the merge base "
                    f"with origin/master. "
                    f"An entry only ever falls: lower the file, not the bar."
                )
        elif not guard_changed and path not in untracked_on_master:
            found.append(
                f"{path}: added to {name} at {limit}, though the merge base already tracks "
                f"the file and the guard is unchanged against it. A file that was "
                f"already there has to meet the cap — split it rather than listing it."
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


def _bound_module_names(
    tree: ast.AST,
    first_party: Collection[str],
    module_fixtures: Collection[str] = (),
) -> set[str]:
    """The local names in `tree` that hold a first-party module.

    `module_fixtures` are conftest fixture names that hand back a module; a function parameter
    spelled with one of them holds that module without any import saying so.
    """
    bound: set[str] = set()
    for node in ast.walk(tree):
        match node:
            case ast.FunctionDef(args=args) | ast.AsyncFunctionDef(args=args):
                bound |= {
                    arg.arg
                    for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs)
                    if arg.arg in module_fixtures
                }
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


def _targets_a_first_party_module(
    target: ast.expr,
    bound: Collection[str],
    first_party: Collection[str],
) -> bool:
    """Whether one `monkeypatch.setattr` target names a first-party module.

    Two spellings, identical at runtime, resolved against different sets. The object form
    walks the attribute chain down to its root name and asks whether an import bound that
    name to a first-party module. The string form carries the module's own dotted name, so
    there is no local binding to read and its root segment is matched against the first-party
    names themselves.
    """
    # DECIDED: the string form counts, resolved against `first_party` rather than `bound`. It
    # is the object form's equal at runtime, and counting only the object form taught authors
    # which spelling the guard could not see. The rationale and the incident are in this
    # module's docstring.
    if isinstance(target, ast.Constant) and isinstance(target.value, str):
        root, _, attr = target.value.partition(".")
        return bool(attr) and root in first_party
    while isinstance(target, ast.Attribute):
        target = target.value
    return isinstance(target, ast.Name) and target.id in bound


def _assigned_attributes(node: ast.AST) -> list[ast.Attribute]:
    """The attribute targets one assignment statement writes, tuple targets unpacked."""
    pending: list[ast.expr]
    match node:
        case ast.Assign(targets=targets):
            pending = list(targets)
        case ast.AugAssign(target=target):
            pending = [target]
        case ast.AnnAssign(target=target, value=ast.expr()):
            pending = [target]
        case _:
            return []
    found: list[ast.Attribute] = []
    while pending:
        target = pending.pop()
        match target:
            case ast.Attribute():
                found.append(target)
            case ast.Tuple(elts=elts) | ast.List(elts=elts):
                pending.extend(elts)
            case ast.Starred(value=value):
                pending.append(value)
    return found


def _restore_nodes(tree: ast.AST) -> set[int]:
    """The `id` of every node inside a `finally:` block.

    A hand-rolled patch saves the attribute, assigns a double and puts the original back in
    `finally`. The restore is an assignment to the same module attribute, so counting it would
    score one patch as two where `monkeypatch.setattr` scores it as one.
    """
    return {
        id(inner)
        for node in ast.walk(tree)
        if isinstance(node, ast.Try | ast.TryStar)
        for stmt in node.finalbody
        for inner in ast.walk(stmt)
    }


def count_module_patches(
    source: str,
    first_party: Collection[str],
    module_fixtures: Collection[str] = (),
) -> int:
    """How many patches in `source` target a first-party module.

    A patch is a `monkeypatch.setattr` call, or an assignment to an attribute of a name bound
    to a first-party module (`mod.attr = double`, `mod.sub.attr = double`). `module_fixtures`
    names the conftest fixtures that hand back a module, so a patch on such a parameter
    counts. The module docstring lists what this deliberately does not see.
    """
    tree = ast.parse(source)
    bound = _bound_module_names(tree, first_party, module_fixtures)
    restores = _restore_nodes(tree)

    counted = 0
    for node in ast.walk(tree):
        match node:
            case ast.Call(
                func=ast.Attribute(value=ast.Name(id="monkeypatch"), attr="setattr"),
                args=[target, *_],
            ):
                if _targets_a_first_party_module(target, bound, first_party):
                    counted += 1
        if id(node) not in restores:
            counted += sum(
                _targets_a_first_party_module(attribute, bound, first_party)
                for attribute in _assigned_attributes(node)
            )
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
