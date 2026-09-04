"""One PR's landing: the state every phase reads and writes, and the ways it ends.

No phase logic lives here. A phase is a function in its own module taking a Landing; this
module is what they share. `pipeline.py`'s docstring records which phase reads and writes
which attribute, so a reader does not have to derive the contract from seven call sites.

TAGS ARE A `list[str]` FOR THE WHOLE LANDING. `resolved_tags` is built as a list by
`classify`, and joined to a comma string only where a subprocess argv or an operator-facing
line needs one -- `tags_csv` is that join. It used to be joined at `classify.py` and split
again at `health_verdict.py`, and it is named `resolved_tags` rather than `tags` because
`Options.tags` is the (unresolved) command-line value and the two shadowed each other.
"""

import subprocess
from collections.abc import Callable
from enum import StrEnum
from typing import Any, NoReturn

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from deploy_tools.land_lib.ledger import Ledger
from deploy_tools.land_lib.options import Options
from deploy_tools.land_lib.outcome import Outcome, say
from deploy_tools.land_lib.tools import Classifier, Tools

BRANCH = "master"


class TickState(StrEnum):
    """What the deployer did with this landing, read from its own marker files.

    `UNKNOWN` is not a deployer state: it is this landing failing to READ one. It exists so
    an unreadable state directory cannot be reported as `CONVERGED`, which is the answer
    that says the tick applied the change.
    """

    HELD = "held"
    BEHIND = "behind"
    CONVERGED = "converged"
    UNKNOWN = "unknown"


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
        self.plane = ""
        self.self_applied = False
        self.remaining_setup = ""
        self.needs_diff = False
        self.deployed_hosts: set[str] = set()

    @property
    def tags_csv(self) -> str:
        """`resolved_tags` as the comma string an argv, a label or a message needs."""
        return ",".join(self.resolved_tags)

    # -- ending the landing -------------------------------------------------------------

    def die(self, msg: str, rc: int = 2, verdict: str | None = None) -> NoReturn:
        """Stop with `land: <msg>` on stderr, and a VERDICT line when one is named."""
        raise Outcome(rc, f"PR #{self.opts.pr} — {msg}", verdict, error=msg)

    def finish(self, verdict: str, rc: int, detail: str) -> NoReturn:
        """Stop with a VERDICT line and nothing on stderr."""
        raise Outcome(rc, detail, verdict)

    # -- shared reads -------------------------------------------------------------------

    def view(self, fields: str) -> dict[str, Any]:
        """`gh pr view --json <fields>` for this PR, or die.

        Three ways the read fails, each named rather than escaping as a traceback: gh
        exiting non-zero, gh not answering inside its timeout, and gh answering with
        something that is not JSON (an auth prompt or a proxy error page). The last two
        would otherwise propagate out of the phase and annotate as `aborted` with no line
        saying which PR read failed.
        """
        try:
            return (
                self.tools.gh_json("pr", "view", self.opts.pr, "--json", fields) or {}
            )
        except subprocess.CalledProcessError as exc:
            self.die(f"could not read PR #{self.opts.pr}: {exc.stderr.strip()}", 1)
        except subprocess.TimeoutExpired:
            self.die(f"could not read PR #{self.opts.pr}: gh timed out", 1)
        except ValueError:
            # json.JSONDecodeError is a ValueError subclass.
            self.die(f"could not read PR #{self.opts.pr}: unparseable gh output", 1)

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        """git in the PRIMARY checkout, never raising; callers read returncode."""
        return self.tools.git(*args, cwd=self.opts.primary, check=False)

    def fetch_branch(self) -> None:
        if self.git("fetch", "-q", "origin", BRANCH).returncode != 0:
            self.die(f"could not fetch origin/{BRANCH}", 1)

    def note_lock_contention(self, seconds: int, holder: str = "") -> None:
        """Book one attempt that lost the tree lock; name the holder on the first.

        `holder` is the sample the caller took BEFORE the losing attempt. By the time the
        attempt returns, the holder has usually released and a fresh read is empty, so the
        given one wins whenever it is non-empty (issue #1031).
        """
        self.ledger.lock_waited += seconds
        if self.ledger.lock_holder:
            return
        self.ledger.lock_holder = holder or self.tools.lock_holder()
        if self.ledger.lock_holder:
            say(f"lock held by {self.ledger.lock_holder}")

    def state(self, name: str) -> str | None:
        """The deployer's `<name>` marker; "" when absent, None when it could not be read."""
        return self.tools.read_state(self.opts.deployer_state, name)

    def tick_state(self) -> TickState:
        """What the deployer did with this landing, from its own markers.

        `hold_sha` non-empty: an apply failed and the tick is holding. `behind_since`
        non-empty: the tick did not cross origin (a newer merge whose CI is still running,
        most often). Both are written by the deployer after main() returns.

        A marker that could not be READ is `UNKNOWN`, never `CONVERGED`: reporting the
        deployer as converged on the strength of a read that failed is the one wrong answer
        that ends a landing with `settled`. `hold_sha` is read first and decides on its
        own: a readable hold is `HELD` whatever `behind_since` does, so an unreadable
        second marker cannot downgrade a real hold to `UNKNOWN`.
        """
        hold = self.state("hold_sha")
        if hold:
            return TickState.HELD
        behind = self.state("behind_since")
        if hold is None or behind is None:
            return TickState.UNKNOWN
        if behind:
            return TickState.BEHIND
        return TickState.CONVERGED


def retry_while_locked(
    ln: Landing, busy_rc: int, attempt: Callable[[], int], note: Callable[[int], str]
) -> int:
    """Run `attempt` until it stops returning `busy_rc`; the last return code.

    The tick and the deploy phase both ride out the same tree lock with the same accounting,
    and had the same loop written out twice. Bounded by `opts.lock_retries`, with
    `opts.lock_backoff` between attempts.

    The holder is sampled BEFORE each attempt on purpose: read afterwards, it has usually
    released and the landing books an empty one (issue #1031). For the deploy phase the
    sample precedes the WHOLE per-host loop, so contention arising at a later host can name
    the holder seen before the first one; `note_lock_contention`'s `holder or ...` fallback
    covers the case where that sample was empty.

    Args:
      ln: the landing, for its budgets and its lock accounting.
      busy_rc: the return code that means "the lock was held; try again".
      attempt: what to run, returning a process exit code.
      note: the progress line for attempt N, given N.
    """
    o, t = ln.opts, ln.tools
    rc = 0
    for n in range(1, o.lock_retries + 1):
        # DECIDED: this samples on every attempt, including an uncontended first one, where
        # bash's `note_lock_contention` only ran fuser+ps after an attempt had already lost
        # the lock. That is a real parity delta (#1085 item 4) and it stays: reverting to
        # bash's post-failure sample reintroduces the #1031 race this pre-sample exists to
        # close, and `test_the_lock_holder_is_sampled_before_the_attempt`
        # (tests/test_land_tick.py) plus its deploy.py sibling would go red on the revert.
        # The cost is two short-lived processes per attempt against a 10-15 minute landing.
        holder = t.lock_holder()
        started = t.clock()
        rc = attempt()
        if rc != busy_rc:
            break
        ln.note_lock_contention(int(t.clock() - started) + o.lock_backoff, holder)
        say(note(n))
        t.sleep(o.lock_backoff)
    return rc
