"""The landing policy a lander unit sets for an agent's PRs, checked before any merge call.

Two environment settings switch it on. A landing that sets neither runs exactly as before,
which is every interactive session and renovate-agent's lander:

- `LAND_REQUIRE_BRANCH_PREFIX`: the head branch must be in this repo, start with the prefix,
  and target master. The agent's GitHub account can push only under its own prefix ("agent
  branch fence", ruleset 24517167), so this keeps the lander to the branches the fence gives
  the agent. The policy binds only a landing: the agent's account can also arm GitHub's
  auto-merge, which skips it, by the operator's choice on 2026-10-11.
- `LAND_APPROVAL_PATHS`: a file of path prefixes, one per line. A PR changing a path under one
  of them is refused. Those are the paths that widen the agent's own authority: its roles, its
  credentials, the rulesets' drift checks and this code. A PR changing a module this landing
  process has imported is refused the same way, and so is a new file that would shadow one
  and any compiled module file; see `gate_hits`. So is a change that can run code on import
  with no import line of its own, such as a non-empty `__init__.py` or a symlink; see
  `shape_hits`.
- `LAND_APPROVER`: a GitHub login. A PR the approval list refuses lands anyway when this
  login's latest review is an approval of the head SHA the checks read. A later
  changes-requested or a dismissal undoes it, and so does a push: the approval then names an
  older commit. Unset, nothing lifts the refusal.

When either of the first two is set, the policy also refuses while the deployer holds a SHA, and `check`
returns the head SHA it checked. `merge.py` pins every merge path to that SHA, so a push after
the checks fails the merge rather than landing unchecked.

WHY IN land.sh AND NOT IN THE LANDER. The operator chose on 2026-10-05 that landing logic lives
here rather than being re-implemented in a unit's own script. The approval list therefore
names `scripts/deploy_tools/land`, so a PR that weakens these checks needs the operator's
approval as well.
"""

import posixpath
import sys as _sys
from importlib import machinery
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path as _Path
from types import ModuleType
from typing import NoReturn

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from deploy_tools.land_lib import gh_read
from deploy_tools.land_lib.landing import BRANCH, Landing
from deploy_tools.land_lib.outcome import hold_clear_hint, say
from deploy_tools.land_lib.pr_json import (
    PrFile,
    PrReview,
    PrView,
    parse_file,
    parse_review,
)
from lib.json_types import JsonObject, as_list, as_object_list

# GitHub's REST files endpoint returns at most 3000 files, so a PR at the cap may hide a path
# the list names.
FILE_CAP = 3000
# The review states that change whether a PR is approved. A COMMENTED review after an
# approval leaves it approved, as it does for GitHub's own review rule.
VERDICT_STATES = ("APPROVED", "CHANGES_REQUESTED", "DISMISSED")
# The checkout this process runs from: the lander's, which only fast-forwards to master.
CHECKOUT = _Path(__file__).resolve().parents[3]
# The site module imports these from any directory on sys.path when the interpreter starts.
STARTUP_MODULES = frozenset({"sitecustomize", "usercustomize"})
# Module files the import system loads that are not `.py` source. A `__pycache__` pyc runs in
# place of its unchanged source, a sourceless `.pyc` imports on its own, and an extension
# module is tried before `.py` in the same directory. None is tracked legitimately, and none
# shows the code it runs in a diff.
COMPILED_SUFFIXES = tuple(
    s for s in machinery.all_suffixes() if s not in machinery.SOURCE_SUFFIXES
)
# The blob id git gives an empty file, which is the only `__init__.py` an agent lands alone.
EMPTY_BLOB = "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
SYMLINK_MODE = "120000"
# The listing statuses that bring a path into the tree under a name it did not have.
ARRIVING = ("added", "renamed", "copied")


def is_compiled_module(path: str) -> bool:
    """Whether `path` is a file the import system would load as compiled code."""
    return "__pycache__" in path.split("/") or path.endswith(COMPILED_SUFFIXES)


def read_approval_paths(path: str) -> list[str]:
    """The prefixes in the approval-path file, without blank lines and `#` comments.

    Raises:
        OSError: the file cannot be read.
        ValueError: the file names no prefix. An empty list would approve every path, so it
            is refused rather than read as "nothing needs approval".
    """
    lines = _Path(path).read_text().splitlines()
    prefixes = [
        s for s in (line.strip() for line in lines) if s and not s.startswith("#")
    ]
    if not prefixes:
        raise ValueError(f"{path} names no path")
    return prefixes


def branch_problems(view: PrView, prefix: str) -> list[str]:
    """Why the PR's head branch is not one the policy lands; empty when it is."""
    problems = []
    if view.get("isCrossRepository") is not False:
        problems.append("its head branch is not in this repo")
    if view.get("baseRefName") != BRANCH:
        problems.append(
            f"it targets {view.get('baseRefName') or '<unknown>'}, not {BRANCH}"
        )
    name = view.get("headRefName") or ""
    if not name.startswith(prefix):
        problems.append(f"its branch {name or '<unknown>'} is outside {prefix}")
    return problems


def approval_hits(files: list[PrFile], prefixes: list[str]) -> list[str]:
    """Each changed path under an approval prefix, in order, without duplicates.

    A renamed file is checked under its old path too, so a rename out of a listed directory
    still needs approval.
    """
    hits: list[str] = []
    for entry in files:
        for name in (entry.get("filename"), entry.get("previous_filename")):
            if name and name not in hits and name.startswith(tuple(prefixes)):
                hits.append(name)
    return hits


def gate_hits(
    files: list[PrFile],
    checkout: _Path,
    modules: Mapping[str, ModuleType | None],
    search_dirs: Iterable[str],
) -> list[str]:
    """Each changed path that would change the code running this landing, in order.

    `modules` and `search_dirs` are `sys.modules` and `sys.path` when the policy runs. By then
    land.py has imported every module the checks use, so the loaded set is the gate's own
    import closure, with no hand-kept list to drift (#3888). A path is a hit when it is the
    source of a loaded module, or when it would import under a name already loaded from a
    directory on `sys.path`. The second form catches a new `scripts/json.py` or
    `scripts/lib/__init__.py`, which would shadow `json` or turn the `lib` namespace package
    into code. A module first imported after the checks cannot change their verdict.

    Every compiled module file is a hit wherever it sits (`is_compiled_module`). A loaded
    module's `__file__` names its `.py` source even when a pyc beside it ran, so neither form
    above would see one.
    """
    loaded: set[str] = set()
    for module in modules.values():
        source = getattr(module, "__file__", None)
        if not source:
            continue
        path = _Path(source).resolve()
        if path.is_relative_to(checkout):
            loaded.add(path.relative_to(checkout).as_posix())
    names = set(modules) | STARTUP_MODULES
    dirs = []
    for entry in search_dirs:
        path = _Path(entry or ".").resolve()
        if path.is_relative_to(checkout):
            prefix = path.relative_to(checkout).as_posix()
            dirs.append("" if prefix == "." else f"{prefix}/")
    hits: list[str] = []
    for entry in files:
        for name in (entry.get("filename"), entry.get("previous_filename")):
            if name and name not in hits and _runs_in_gate(name, loaded, names, dirs):
                hits.append(name)
    return hits


def _runs_in_gate(
    path: str, loaded: set[str], names: set[str], dirs: list[str]
) -> bool:
    if path in loaded or is_compiled_module(path):
        return True
    if not path.endswith(".py"):
        return False
    for prefix in dirs:
        if path.startswith(prefix):
            dotted = path[len(prefix) : -len(".py")].replace("/", ".")
            if dotted.removesuffix(".__init__") in names:
                return True
    return False


def import_roots(checkout: _Path, search_dirs: Iterable[str]) -> list[str]:
    """The directories on `search_dirs` inside `checkout`, as repo-relative paths, in order.

    `search_dirs` is `sys.path` once land.py has imported its modules, so the roots are the
    lander's own inserts rather than a list kept by hand. The checkout root itself is left
    out: it is on `sys.path` only under `python -c` or pytest, never when land.py runs.
    """
    roots: list[str] = []
    for entry in search_dirs:
        path = _Path(entry or ".").resolve()
        if path.is_relative_to(checkout) and path != checkout:
            root = path.relative_to(checkout).as_posix()
            if root not in roots:
                roots.append(root)
    return roots


def _holds_python(tree: Mapping[str, str], directory: str) -> bool:
    prefix = f"{directory}/" if directory else ""
    return any(p.startswith(prefix) and p.endswith(".py") for p in tree)


def shape_hits(
    files: list[PrFile], tree: Mapping[str, str], roots: list[str]
) -> list[str]:
    """Each change that can run code on import with no import line of its own, in order.

    `gate_hits` sees only the modules this landing has already loaded. Code that runs on
    import without being imported by name stays invisible to it while nothing loads it, and a
    later PR the operator approves can then pull it into the closure without showing it. Each
    hit names a path and the rule it broke:

    - a listed `__init__.py` whose blob is not empty, which a SHA comparison decides without
      reading the file;
    - an `__init__.py` that leaves its directory, removed or renamed away, while the
      directory still holds a `.py` file at any depth. Deleting or moving the whole package
      leaves no `.py` behind, so it passes;
    - a listed path that is a symlink at the head, or that the head tree lacks;
    - an added `__main__.py`;
    - a module added or renamed in beside a directory of the same name, at any depth;
    - a module added or renamed in at the top of one import root whose name matches a
      directory or a module at the top of another root.

    Args:
        files: the PR's REST file listing.
        tree: every path at the PR head the policy pinned, to its git mode.
        roots: the repo-relative `sys.path` directories, from `import_roots`.
    """
    dirs = {posixpath.dirname(p) for p in tree}
    for d in list(dirs):
        while d:
            d = posixpath.dirname(d)
            dirs.add(d)
    hits: list[str] = []

    def hit(path: str, why: str) -> None:
        line = f"{path} ({why})"
        if line not in hits:
            hits.append(line)

    for entry in files:
        status, name = entry.get("status", ""), entry.get("filename", "")
        previous = entry.get("previous_filename", "")
        base = posixpath.basename(name)
        if status != "removed":
            if base == "__init__.py" and entry.get("sha") != EMPTY_BLOB:
                hit(name, "a non-empty __init__.py")
            if base == "__main__.py":
                hit(name, "a __main__.py")
            if name not in tree:
                hit(name, "missing from the head tree")
            elif tree[name] == SYMLINK_MODE:
                hit(name, "a symlink")
        leaving = name if base == "__init__.py" and status == "removed" else ""
        if previous != name and posixpath.basename(previous) == "__init__.py":
            leaving = previous
        if leaving and _holds_python(tree, posixpath.dirname(leaving)):
            hit(
                leaving, "an __init__.py left a directory that still holds Python files"
            )
        if status not in ARRIVING or not name.endswith(".py"):
            continue
        module = name.removesuffix(".py")
        if module in dirs:
            hit(name, "a module beside a same-named directory")
        parent, stem = posixpath.split(module)
        if parent not in roots:
            continue
        for other in roots:
            for shadowed in (f"{other}/{stem}", f"{other}/{stem}.py"):
                if other != parent and (shadowed in dirs or shadowed in tree):
                    hit(name, f"shadows {shadowed} in another import root")
    return hits


def head_tree(ln: Landing, head: str) -> dict[str, str]:
    """Every path at `head` to its git mode, or die refusing when the tree cannot be read.

    The primary checkout fetches the PR's pull ref first, because it holds only master. The
    tree is then read at the pinned `head` rather than at what the fetch returned, so a push
    since the checks began cannot change it. `ls-tree` has no truncation limit, which the REST
    tree endpoint has. A failed fetch is not itself a refusal: a head fetched earlier still
    reads.
    """
    ln.git(
        "fetch", "-q", "--no-write-fetch-head", "origin", f"refs/pull/{ln.opts.pr}/head"
    )
    listed = ln.git("ls-tree", "-r", "-z", "--full-tree", head)
    tree: dict[str, str] = {}
    if listed.returncode == 0:
        for record in listed.stdout.split("\0"):
            meta, _, path = record.partition("\t")
            if path:
                tree[path] = meta.split(" ", 1)[0]
    if not tree:
        _refuse(ln, f"could not read the tree at the PR head {head[:8]}")
    return tree


def approval_problem(reviews: list[PrReview], approver: str, head: str) -> str:
    """Why `approver` has not approved `head`; empty when their latest verdict approves it."""
    theirs = sorted(
        (
            r
            for r in reviews
            if (r.get("user") or {}).get("login") == approver
            and r.get("state") in VERDICT_STATES
        ),
        key=lambda r: r.get("submitted_at") or "",
    )
    if not theirs:
        return f"{approver} has not approved it"
    latest = theirs[-1]
    state = latest.get("state", "")
    if state != "APPROVED":
        return f"{approver}'s latest review is {state.lower()}"
    approved = latest.get("commit_id") or ""
    if approved != head:
        return f"{approver} approved {approved[:8] or '<unknown>'}, not the head {head[:8]}"
    return ""


def _refuse(ln: Landing, why: str) -> NoReturn:
    ln.die(f"refused by the landing policy: {why}", 1)


def _list[T](ln: Landing, what: str, parse: Callable[[JsonObject], T]) -> list[T]:
    """Every entry of the PR's REST `what` listing (`files`, `reviews`), across all pages.

    `parse` runs inside the read, so an entry of the wrong shape refuses the landing as
    unparseable gh output rather than escaping as a traceback.
    """
    url = f"repos/{{owner}}/{{repo}}/pulls/{ln.opts.pr}/{what}"

    def call() -> list[T]:
        got = ln.tools.gh_json("api", "--paginate", "--slurp", url)
        return [
            parse(entry)
            for page in as_list(got or [], f"gh api pulls/{what}")
            for entry in as_object_list(page, f"gh api pulls/{what} page")
        ]

    try:
        return gh_read.read(call, ln.tools.sleep, f"gh api pulls/{what}")
    except gh_read.GhReadFailed as exc:
        _refuse(ln, f"could not list the PR's {what}: {exc}")
    except ValueError as exc:
        _refuse(ln, f"could not list the PR's {what}: unparseable gh output: {exc}")


def check(ln: Landing) -> str:
    """Run every policy check, or die refusing; the head SHA the checks read.

    The head is read again after the file list, so the list and the returned SHA describe the
    same commit.
    """
    o = ln.opts
    view = ln.view("headRefOid,headRefName,baseRefName,isCrossRepository")
    head = view.get("headRefOid") or ""
    if not head:
        _refuse(ln, "GitHub returned no head SHA to pin the merge to")
    if o.require_branch_prefix:
        problems = branch_problems(view, o.require_branch_prefix)
        if problems:
            _refuse(ln, "; ".join(problems))
    hold = ln.state("hold_sha")
    if hold is None:
        _refuse(ln, "the deployer's hold_sha could not be read")
    if hold:
        _refuse(
            ln,
            f"the deployer is holding {hold}; a merge now would deploy on top of a failed "
            f"apply. {hold_clear_hint(hold)}",
        )
    if o.approval_paths:
        try:
            prefixes = read_approval_paths(o.approval_paths)
        except (OSError, ValueError) as exc:
            _refuse(ln, f"the approval-path list is unusable: {exc}")
        # The REST listing, because it also names a rename's old path.
        files = _list(ln, "files", parse_file)
        if len(files) >= FILE_CAP:
            _refuse(ln, f"it changes {len(files)} files, at GitHub's listing cap")
        hits = approval_hits(files, prefixes)
        hits += [
            path
            for path in gate_hits(files, CHECKOUT, _sys.modules, _sys.path)
            if path not in hits
        ]
        hits += shape_hits(
            files, head_tree(ln, head), import_roots(CHECKOUT, _sys.path)
        )
        if hits:
            why = (
                f"it changes paths that need the operator's approval: {', '.join(hits)}"
            )
            if not o.approver:
                _refuse(ln, why)
            problem = approval_problem(
                _list(ln, "reviews", parse_review), o.approver, head
            )
            if problem:
                _refuse(ln, f"{why}; {problem}")
            say(f"{o.approver} approved {head[:8]}, lifting the approval-path refusal")
            ln.approved_head = head
    again = ln.view("headRefOid").get("headRefOid") or ""
    if again != head:
        _refuse(
            ln, f"the head moved during the checks ({head[:8]} -> {again[:8]}); re-run"
        )
    say(f"landing policy passed at {head[:8]}")
    return head
