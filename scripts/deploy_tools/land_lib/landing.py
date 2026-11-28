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
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, NoReturn


from deploy_tools.land_lib.ledger import Ledger
from deploy_tools.land_lib.options import Options
from deploy_tools.land_lib.outcome import Outcome
from deploy_tools.land_lib.pr_json import PrView, parse_view, rest_filter
from deploy_tools.land_lib import gh_read
from deploy_tools.land_lib.tools import Classifier, Tools
from lib import gh_rate_limit
from lib.json_types import as_object

BRANCH = "master"

# Seconds between attempts at fetching origin/master: three attempts in all. A fetch that fails
# once has answered seconds later.
FETCH_BACKOFF_S = (5, 15)


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
        plane: what a hand must apply, "" for nothing: the note a rotated secret prints.
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
        # Set once step 4 starts; the health verdict reads it to gate from a tree holding this PR.
        self.deployed_at = ""
        # Set by `merge.arm_merge` to the squash subject; empty means something else (such as an
        # armed auto-merge) merges and this landing only polls.
        self.direct_merge_subject = ""
        # The head SHA the landing policy checked; every merge path is pinned to it. Empty when no
        # policy is set.
        self.pinned_head = ""
        # `merge.await_merge` never updates this head's branch: the update would make a head the
        # approval does not name.
        self.approved_head = ""
        # The master commit and head a landing batch stacked this PR on; while both hold, no branch
        # update or stale-base check is needed.
        self.tested_on = ""
        self.tested_head = ""
        self.batch: list[Any] = []
        # The seconds the run of this landing's batch took, which its merge wait counts.
        self.batched_s = 0
        # The seconds `merge.await_merge` took to merge the PR, the wait for the turn included.
        self.merge_wait_s = 0
        # Set once a batch that tested this landing's PR was not green: it forms no batch.
        self.lands_alone = False
        self.split_members: list[Any] = []
        self.serial_lock: Callable[[], None] | None = None
        # Frees this landing's slot before its verdict; `slots.held` sets it while one is held.
        self.give_up_slot: Callable[[], None] = lambda: None
        # Set only while this landing holds the turn, so never for a batch member.
        self.give_back_turn: Callable[[], None] | None = None
        self.step_out_of_turn: Callable[[Callable[[], Any], str], Any] | None = None
        # Set once a wait stepped out of the turn, because master may have moved; the merge wait
        # clears it.
        self.turn_interrupted = False
        # The head its landing stands in the line with while it waits for the turn; "" once the
        # PR is marked ready, or for a head that is no draft.
        self.line_head = ""
        self.hand_over_turn: Callable[[str], None] | None = None
        # The head `changed_paths` last read the PR's file list at, and that list.
        self._changed: tuple[str, tuple[str, ...]] | None = None

    @property
    def plane(self) -> str:
        """What a hand must apply, or ""."""
        return self.classification.plane

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

        `rc` has no default: one of 2 collided with three other meanings of 2, so
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

    def read[T](self, call: Callable[[], T], label: str) -> T:
        """`call()`, a gh read retried and waited out as `gh_read.read` does.

        Each rate-limit wait runs through `without_turn`.

        Raises:
            gh_read.GhReadFailed: GitHub did not answer, after `gh_read`'s retries and waits.
        """
        return gh_read.read(call, self.tools.sleep, label, self.without_turn)

    def without_turn[T](
        self, wait: Callable[[], T], reason: str = "to wait out GitHub's rate limit"
    ) -> T:
        """`wait()`, with the landing turn given back for its length when this landing holds it.

        A landing that holds the turn gives it back, so no landing behind it waits through the
        same wait, and asks for it again once `wait` returns, so the work that waited goes on
        in the turn. It then sets `turn_interrupted`. The log names `reason`, which completes
        "gave the landing turn back".
        """
        if self.step_out_of_turn:
            return self.step_out_of_turn(wait, reason)
        return wait()

    def view(self, fields: str) -> PrView:
        """This PR's `fields`, in the shape `gh pr view --json <fields>` gives, or die.

        The answer comes from the REST pull object when it carries every field, and from
        `gh pr view` otherwise (`pr_json.rest_filter`).

        Three ways the read fails, each named rather than escaping as a traceback: gh
        exiting non-zero, gh not answering inside its timeout, and gh answering with
        something that is not JSON (an auth prompt or a proxy error page) or JSON of the wrong
        shape. The last two
        would otherwise propagate out of the phase and annotate as `aborted` with no line
        saying which PR read failed.

        A timeout or a 5xx is retried first (`gh_read`).
        """
        pr = self.opts.pr
        rest = rest_filter(fields)
        label = f"gh api pulls/{pr}" if rest else f"gh pr view #{pr}"

        def call() -> PrView:
            if rest:
                url = f"repos/{{owner}}/{{repo}}/pulls/{pr}"
                got = self.tools.gh_json("api", url, "--jq", rest)
            else:
                got = self.tools.gh_json("pr", "view", pr, "--json", fields)
            return parse_view(as_object(got or {}, label))

        try:
            return self.read(call, label)
        except gh_read.GhReadFailed as exc:
            self.die(f"could not read PR #{pr}: {exc}", 1)
        except ValueError as exc:
            # json.JSONDecodeError is a ValueError subclass.
            self.die(f"could not read PR #{pr}: unparseable gh output: {exc}", 1)

    def changed_paths(self, head: str) -> list[str]:
        """The paths the PR changes, as `gh pr view` lists them with the PR at `head`.

        The list is read from GitHub once per head: a call for the head the last read found
        returns that read. The merge wait reads it before it asks for the landing turn
        (`merge.await_merge`), so a rule the turn runs finds it here without a read of its own.

        Raises:
            gh_read.GhReadFailed: GitHub did not answer, after `gh_read`'s retries and waits.
            ValueError: gh answered with something that is not a PR's file list.
        """
        if self._changed and self._changed[0] == head:
            return list(self._changed[1])
        pr = self.opts.pr
        label = f"gh pr view #{pr} files"

        def call() -> PrView:
            got = self.tools.gh_json("pr", "view", pr, "--json", "files,headRefOid")
            return parse_view(as_object(got, label))

        view = self.read(call, label)
        paths = tuple(f["path"] for f in view.get("files", []))
        self._changed = (view.get("headRefOid", ""), paths)
        return list(paths)

    def remedy(self, why: str, otherwise: str) -> str:
        """The next step after GitHub refused a write with `why`: `otherwise`, or a rate limit's.

        A rate limit's remedy is to wait and re-run the landing (`gh_rate_limit.remedy`),
        because `otherwise` repeats the call the limit refused.
        """
        return gh_rate_limit.remedy(why, self.read_quota) or otherwise

    def read_quota(self, name: str) -> object:
        """One quota's `.resources.<name>` object from `gh api rate_limit`, which costs none."""
        return self.tools.gh_json("api", "rate_limit", "--jq", f".resources.{name}")

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        """git in the PRIMARY checkout, never raising; callers read returncode."""
        return self.tools.git(*args, cwd=self.opts.primary, check=False)

    def fetch_branch(self) -> None:
        """Fetch origin/master into the primary checkout, retried after each failed attempt.

        Raises:
            Outcome: `refused`, once every attempt failed. With `merge_sha` set the PR is
              already merged, so the detail names the merge commit and the re-run that follows
              it through; otherwise it says only that the fetch failed.
        """
        for wait in (*FETCH_BACKOFF_S, None):
            if self.git("fetch", "-q", "origin", BRANCH).returncode == 0:
                return
            if wait is not None:
                self.tools.sleep(wait)
        why = f"could not fetch origin/{BRANCH} in {len(FETCH_BACKOFF_S) + 1} attempts"
        if self.merge_sha:
            self.die(
                f"merged as {self.merge_sha[:8]}, deploy not followed: {why}; "
                f"re-run land.sh --pr {self.opts.pr} to follow the merge through",
                1,
            )
        self.die(why, 1)
