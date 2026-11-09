"""One PR's landing: the state every phase reads and writes, and the ways it ends.

No phase logic lives here. A phase is a function in its own module taking a Landing; this
module is what they share. `pipeline.py`'s docstring records which phase reads and writes
which attribute, so a reader does not have to derive the contract from seven call sites.

TAGS ARE A `list[str]` FOR THE WHOLE LANDING. `resolved_tags` is built as a list by
`classify`, and joined to a comma string only where a subprocess argv or an operator-facing
line needs one -- `tags_csv` is that join. It is named `resolved_tags` rather than `tags`
because `Options.tags` is the (unresolved) command-line value and the two would shadow each
other.
"""

import subprocess
from dataclasses import dataclass
from typing import Any, NoReturn

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from deploy_tools.land_lib.ledger import Ledger
from deploy_tools.land_lib.options import Options
from deploy_tools.land_lib.outcome import Outcome
from deploy_tools.land_lib.pr_json import PrView, parse_view
from deploy_tools.land_lib import gh_read
from deploy_tools.land_lib.tools import Classifier, Tools
from lib.json_types import as_object

BRANCH = "master"


@dataclass(frozen=True)
class Classification:
    """What `classify.classify` derives from the PR's paths and range, which no phase rewrites.

    Every field is a function of the PR's file list, its recorded range and the tags declared
    at the merge commit, all read in step 1. The tags are not here: step 5 re-derives them
    from the diff when `gh` truncated the file list, so they stay a phase output on `Landing`.

    Attributes:
        pr_paths: the PR's changed paths, as `gh pr view` listed them.
        pr_range: `<merge-base>..<pr-head>`, or "" when it could not be read.
        quiet: the broad paths whose diff carries no content change.
        declared: the tags declared at the merge commit; None when that read failed, and
            every reader then falls back to its own tree.
        plane: what a hand must apply, as step 1 renders it with whole-role tags; "" for
            nothing. `Landing.plane` is the note to print.
    """

    pr_paths: tuple[str, ...] = ()
    pr_range: str = ""
    quiet: frozenset[str] = frozenset()
    declared: frozenset[str] | None = None
    plane: str = ""

    def declared_set(self) -> set[str] | None:
        """`declared` as the mutable set the classifiers take, or None when the read failed."""
        return None if self.declared is None else set(self.declared)


class Landing:
    """The state of one landing, plus the shared helpers phases call."""

    def __init__(
        self, opts: Options, tools: Tools, classifier: Classifier | None = None
    ) -> None:
        self.opts = opts
        self.tools = tools
        self.classifier = classifier or Classifier()
        self.ledger = Ledger(pr=opts.pr, t_start=tools.clock())
        self.merge_sha = ""
        self.resolved_tags = [t for t in opts.tags.split(",") if t]
        self.classification = Classification()
        # The plane note rendered again with the narrow tags the deployer recorded for this
        # PR's range, by `classify.narrow_plane`; None until then, or when nothing narrowed.
        self.narrowed_plane: str | None = None
        self.warnings: list[str] = []  # `warn` fills it; `pipeline.run` reports it
        self.needs_diff = False
        self.host_candidate = False  # a path is under HOST_INPUTS
        self.host_targets: list[Any] = []  # HostTargets; host_wait imports us
        self.flux_tags: list[
            str
        ] = []  # the k8s tags a promotion applies and step 6 gates
        self.suspended_tags: list[str] = []  # suspended at M; the gate skips them
        self.named_k8s_tags: list[
            str
        ] = []  # the k8s tags the PR's paths or --tags named
        # The merge commit, set once step 4 starts: the primary checkout may not carry it
        # yet. The health verdict reads it to gate from a tree holding this PR.
        self.deployed_at = ""
        # Set by `merge.arm_merge` to the squash subject. `merge.await_merge` then merges the
        # PR directly once CI is green, at the head CI checked. Empty means something else
        # does the merge, such as an auto-merge a session armed, and this landing only polls.
        self.direct_merge_subject = ""
        # The head SHA the landing policy checked (`policy.check`), which every merge path is
        # then pinned to. Empty when no policy is set, and the merge takes whatever head it finds.
        self.pinned_head = ""
        # The head SHA whose approval let the landing policy admit a PR on its approval list.
        # `merge.await_merge` never updates this head's branch: the update would make a head
        # the approval does not name.
        self.approved_head = ""

    @property
    def plane(self) -> str:
        """What a hand must apply, or "": the narrowed note once there is one, else step 1's."""
        if self.narrowed_plane is None:
            return self.classification.plane
        return self.narrowed_plane

    @property
    def tags_csv(self) -> str:
        """`resolved_tags` as the comma string an argv, a label or a message needs."""
        return ",".join(self.resolved_tags)

    # -- ending the landing -------------------------------------------------------------

    def warn(self, text: str) -> None:
        """Report a failure that keeps the verdict: a `== ` line now, the verdict's detail later."""
        # One line, because `handoff._relay` filters by line and `verdict_in` reads only one.
        line = " ".join(text.split())
        print(f"== {line}", flush=True)
        self.warnings.append(line)

    def die(self, msg: str, rc: int, verdict: str | None = None) -> NoReturn:
        """Stop with a VERDICT line naming `msg` on stdout, or `land: <msg>` on stderr without one.

        `rc` has no default: one of 2 collided with three other meanings of 2 (#4451), so
        every caller names a code land.sh documents. An exit-1 stop with no `verdict` prints
        `refused` (`Outcome`); exit 64 prints no VERDICT line.
        """
        raise Outcome(rc, f"PR #{self.opts.pr} — {msg}", verdict, error=msg)

    def finish(
        self, verdict: str, rc: int, detail: str, recorded: str = ""
    ) -> NoReturn:
        """Stop with a VERDICT line and nothing on stderr; `recorded` is `Outcome`'s."""
        raise Outcome(rc, detail, verdict, recorded=recorded)

    # -- shared reads -------------------------------------------------------------------

    def view(self, fields: str) -> PrView:
        """`gh pr view --json <fields>` for this PR, or die.

        Three ways the read fails, each named rather than escaping as a traceback: gh
        exiting non-zero, gh not answering inside its timeout, and gh answering with
        something that is not JSON (an auth prompt or a proxy error page) or JSON of the wrong
        shape. The last two
        would otherwise propagate out of the phase and annotate as `aborted` with no line
        saying which PR read failed.

        A timeout or a 5xx is retried first (`gh_read`, #5503).
        """
        pr = self.opts.pr

        def call() -> PrView:
            got = self.tools.gh_json("pr", "view", pr, "--json", fields)
            return parse_view(as_object(got or {}, "gh pr view"))

        try:
            return gh_read.read(call, self.tools.sleep, f"gh pr view #{pr}")
        except gh_read.GhReadFailed as exc:
            self.die(f"could not read PR #{pr}: {exc}", 1)
        except ValueError as exc:
            # json.JSONDecodeError is a ValueError subclass.
            self.die(f"could not read PR #{pr}: unparseable gh output: {exc}", 1)

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        """git in the PRIMARY checkout, never raising; callers read returncode."""
        return self.tools.git(*args, cwd=self.opts.primary, check=False)

    def fetch_branch(self) -> None:
        if self.git("fetch", "-q", "origin", BRANCH).returncode != 0:
            self.die(f"could not fetch origin/{BRANCH}", 1)

    def state(self, name: str) -> str | None:
        """The deployer's `<name>` marker; "" when absent, None when it could not be read."""
        return self.tools.read_state(self.opts.deployer_state, name)
