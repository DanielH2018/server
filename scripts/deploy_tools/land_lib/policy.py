"""The landing policy a lander unit sets for an agent's PRs, checked before any merge call.

Two environment settings switch it on. A landing that sets neither runs exactly as before,
which is every interactive session and renovate-agent's lander:

- `LAND_REQUIRE_BRANCH_PREFIX`: the head branch must be in this repo, start with the prefix,
  and target master. The agent's GitHub account can push only under its own prefix ("agent
  branch fence", ruleset 24517167), so this keeps the lander to the branches the fence gives
  the agent.
- `LAND_APPROVAL_PATHS`: a file of path prefixes, one per line. A PR changing a path under one
  of them is refused. Those are the paths that widen the agent's own authority: its roles, its
  credentials, the rulesets' drift checks and this code. A PR changing a module this landing
  process has imported is refused the same way, and so is a new file that would shadow one;
  see `gate_hits`.
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

import subprocess
import sys as _sys
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path as _Path
from types import ModuleType
from typing import NoReturn

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from deploy_tools.land_lib.landing import BRANCH, Landing
from deploy_tools.land_lib.outcome import say
from deploy_tools.land_lib.pr_json import (
    PrFile,
    PrReview,
    PrView,
    parse_file,
    parse_review,
)
from lib.json_types import JsonObject, as_list, as_object_list
from lib.repo_paths import GITOPS_DEPLOY_FILES

_sys.path.insert(0, str(GITOPS_DEPLOY_FILES))
from gitops_markers import HOLD_CLEAR_CMD

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
    if path in loaded:
        return True
    if not path.endswith(".py"):
        return False
    for prefix in dirs:
        if path.startswith(prefix):
            dotted = path[len(prefix) : -len(".py")].replace("/", ".")
            if dotted.removesuffix(".__init__") in names:
                return True
    return False


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
    try:
        pages = as_list(
            ln.tools.gh_json(
                "api",
                "--paginate",
                "--slurp",
                f"repos/{{owner}}/{{repo}}/pulls/{ln.opts.pr}/{what}",
            )
            or [],
            f"gh api pulls/{what}",
        )
        return [
            parse(entry)
            for page in pages
            for entry in as_object_list(page, f"gh api pulls/{what} page")
        ]
    except subprocess.CalledProcessError as exc:
        _refuse(ln, f"could not list the PR's {what}: {exc.stderr.strip()}")
    except subprocess.TimeoutExpired:
        _refuse(ln, f"could not list the PR's {what}: gh timed out")
    except ValueError:
        _refuse(ln, f"could not list the PR's {what}: unparseable gh output")


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
            "apply. Once every held plane is applied, clear the hold with Clear in the deploy "
            f"UI or `{HOLD_CLEAR_CMD} {hold}` (CLAUDE.md, When to wait)",
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
