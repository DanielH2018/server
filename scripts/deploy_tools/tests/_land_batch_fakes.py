"""A model of the repository for the landing-batch tests.

`GitHub` holds the PRs, master, the `queue/` branch and the batch's pull request. It answers
the calls a batch makes and the per-PR reads, and hands every other call to the shared
fakes in `_land_fakes.py`.
"""

import subprocess
from itertools import pairwise
from typing import Any

from deploy_tools.land_lib import batch, line, pr_json, turn
from deploy_tools.land_lib.tools import CiVerdict
from lib.exit_codes import CI_GREEN, CI_PENDING, CI_RED

OWN = "999"
MASTER = "0" * 40
GREEN = (CI_GREEN, "CI green")
# The REST-answerable field lists `Landing.view` is asked for, by the filter it sends.
_REST = {
    pr_json.rest_filter(fields): fields
    for fields in (
        "state,mergeable,headRefOid",
        "isDraft,state,mergeable,headRefOid",
        "isDraft",
        "state,headRefOid",
        "headRefOid",
    )
}


def head_of(pr: str) -> str:
    return f"{int(pr):040x}"


def _refused(status: int, message: str) -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(
        1, "gh", stderr=f"gh: {message} (HTTP {status})"
    )


class GitHub:
    """The repository as a batch reads and changes it."""

    def __init__(
        self,
        tools,
        prs,
        run,
        conflicts=(),
        stack_conflicts=(),
        refuse="",
        crash="",
        unmergeable=(),
        stays_draft=False,
        own_red=(),
    ):
        self._tools = tools
        # Each PR as `gh pr view` shows it, by number.
        self.prs: dict[str, dict[str, Any]] = {
            pr: {
                "state": "OPEN",
                "isDraft": True,
                "mergeable": "MERGEABLE",
                "headRefOid": head_of(pr),
                "title": f"PR {pr}",
                "body": "",
                "reviewDecision": "",
                "baseRefName": "master",
                "isCrossRepository": False,
                "commits": [],
                "mergeCommit": None,
                "files": [{"path": "docs/notes.md"}],
                **over,
            }
            for pr, over in prs.items()
        }
        self.master = MASTER
        self.branches: dict[str, str] = {}
        # Every branch that was ever created, and what was merged into each, in order.
        self.stacked: dict[str, list[str]] = {}
        self.batch_prs: list[dict[str, str]] = []
        self.labels: dict[str, set[str]] = {pr: set() for pr in prs}
        # Every PR that ever carried a label, and every PR converted to draft and back.
        self.ever_labelled: set[str] = set()
        self.full_runs: list[str] = []
        # Each PR's labels at the moment it was marked ready, which is when its run starts.
        self.labels_at_ready: dict[str, set[str]] = {}
        self.merged: list[str] = []
        self._run = list(run)
        # Heads that conflict with master, and heads that conflict only with another PR's head.
        self._conflicts = set(conflicts)
        self._stack_conflicts = set(stack_conflicts)
        self._refuse = refuse
        self._crash = crash
        self._unmergeable = set(unmergeable)
        # Whether an unmergeable PR also refuses the ready flip, and so stays a draft.
        self._stays_draft = stays_draft
        # The PRs whose own run, on their head alone, is red.
        self._own_red = set(own_red)
        self._commits = 0
        # The shared fakes' record of every call the model handed on to them.
        self.calls: list = []

    def _commit(self) -> str:
        self._commits += 1
        return f"{self._commits:x}".rjust(40, "c")

    def _pr_of(self, head: str) -> str | None:
        return next((n for n, p in self.prs.items() if p["headRefOid"] == head), None)

    def gh_json(self, *args, **kwargs):
        if args[:2] == ("pr", "view"):
            fields = args[args.index("--json") + 1].split(",")
            return {field: self.prs[args[2]][field] for field in fields}
        if args[0] != "api":
            return self._tools.gh_json(*args, **kwargs)
        method = args[args.index("-X") + 1] if "-X" in args else "GET"
        path = next(a for a in args[1:] if a.startswith("repos/")).split("/", 3)[-1]
        sent = dict(
            field.split("=", 1) for flag, field in pairwise(args) if flag == "-f"
        )
        if self._refuse == f"{method} {path}":
            raise _refused(502, "Bad Gateway")
        if self._crash == f"{method} {path}":
            raise RuntimeError("a bug in the batch")
        if path.startswith("pulls/") and method == "GET" and "/" not in path[6:]:
            pr = self.prs[path.removeprefix("pulls/")]
            if args[-1] in _REST:
                return {field: pr[field] for field in _REST[args[-1]].split(",")}
            merged = pr["state"] == "MERGED"
            # The REST fields the batch asks for by name.
            return {
                "state": "open" if pr["state"] == "OPEN" else "closed",
                "draft": pr["isDraft"],
                "merged": merged,
                "sha": pr["mergeCommit"]["oid"] if merged else None,
            }
        if path == "git/ref/heads/master":
            return {"sha": self.master}
        if (method, path) == ("POST", "git/refs"):
            name = sent["ref"].removeprefix("refs/heads/")
            self.branches[name] = sent["sha"]
            self.stacked[name] = []
            return None
        if method == "DELETE" and path.startswith("git/refs/heads/"):
            if self.branches.pop(path.removeprefix("git/refs/heads/"), None) is None:
                raise _refused(422, "Reference does not exist")
            return None
        if (method, path) == ("POST", "merges"):
            if sent["head"] in self._conflicts or (
                sent["head"] in self._stack_conflicts and self.stacked[sent["base"]]
            ):
                raise _refused(409, "Merge conflict")
            self.stacked[sent["base"]].append(sent["head"])
            self.branches[sent["base"]] = self._commit()
            return {"sha": self.branches[sent["base"]]}
        if (method, path) == ("POST", "pulls"):
            self.batch_prs.append(sent)
            return {"number": 5000}
        if path.startswith("issues/") and "/labels" in path:
            pr = path.split("/")[1]
            if method == "POST":
                self.labels[pr].add(sent["labels[]"])
                self.ever_labelled.add(pr)
            else:
                self.labels[pr].discard(path.rsplit("/", 1)[1])
            return None
        return self._tools.gh_json(*args, **kwargs)

    def gh(self, *args, **kwargs):
        if args[:3] == ("pr", "ready", "--undo"):
            self.prs[args[3]]["isDraft"] = True
            self.full_runs.append(args[3])
        elif args[:2] == ("pr", "ready"):
            if args[2] in self._unmergeable and self._stays_draft:
                raise _refused(422, "could not mark ready")
            self.prs[args[2]]["isDraft"] = False
            self.labels_at_ready[args[2]] = set(self.labels[args[2]])
        elif "PUT" in args and args[3].endswith("/merge"):
            pr = args[3].split("/")[-2]
            # GitHub merges neither a draft nor a head other than the one named.
            sent = dict(f.split("=", 1) for flag, f in pairwise(args) if flag == "-f")
            if pr in self._unmergeable or self.prs[pr]["isDraft"]:
                raise _refused(405, "Pull Request is not mergeable")
            if sent["sha"] != self.prs[pr]["headRefOid"]:
                raise _refused(409, "Head branch was modified")
            self.master = self._commit()
            self.prs[pr].update(state="MERGED", mergeCommit={"oid": self.master})
            self.merged.append(pr)
        else:
            return self._tools.gh(*args, **kwargs)
        return subprocess.CompletedProcess(args, 0, "", "")

    def await_ci(self, sha, timeout, on_checks):
        """A ready PR's own head is green, or red if `own_red` names it; a batch follows `run`."""
        pr = self._pr_of(sha)
        if pr:
            if self.prs[pr]["isDraft"]:
                return CiVerdict(CI_PENDING, "draft")
            return CiVerdict(*((CI_RED, "CI RED") if pr in self._own_red else GREEN))
        verdict = self._run[0]
        if len(self._run) > 1:
            self._run.pop(0)
        return CiVerdict(*verdict)


def land(ln) -> None:
    """The merge wait as the pipeline runs it."""
    with turn.held(ln) as take_turn:
        batch.land(ln, take_turn)


def ticket_of(pr: str) -> str:
    """The ticket of a draft that asked for the turn after the holder, ordered by its number."""
    return str(2**62 + int(pr))


def answers(the_line, *prs) -> list[str]:
    """What each PR's own landing reads in its place on its next poll."""
    replies = [the_line("stand", pr, head_of(pr), ticket_of(pr)) for pr in prs]
    return [(reply.splitlines()[0].split()[1:] or [""])[0] for reply in replies]


def standing(the_line) -> list[tuple[str, str]]:
    """The PR and head of each draft that stands in the line unanswered."""
    rows = the_line("waiting", str(line.FRESH_S)).splitlines()
    return [(pr, head) for pr, head, _ in (row.split() for row in rows)]
