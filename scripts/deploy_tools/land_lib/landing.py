"""One PR's landing: the state every phase reads and writes, and the ways it ends.

No phase logic lives here. A phase is a function in its own module taking a Landing; this
module is what they share.
"""

from __future__ import annotations

import subprocess
from typing import Any, NoReturn

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from deploy_tools.land_lib.ledger import Ledger
from deploy_tools.land_lib.options import Options
from deploy_tools.land_lib.outcome import Outcome, say
from deploy_tools.land_lib.tools import Tools

BRANCH = "master"


class Landing:
    """The state of one landing, plus the shared helpers phases call."""

    def __init__(self, opts: Options, tools: Tools) -> None:
        self.opts = opts
        self.tools = tools
        self.ledger = Ledger(pr=opts.pr, t_start=tools.clock())
        self.merge_sha = ""
        self.tags = opts.tags
        self.plane = ""
        self.self_applied = False
        self.remaining_setup = ""
        self.needs_diff = False
        self.deployed_hosts: set[str] = set()

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

    def state(self, name: str) -> str:
        return self.tools.read_state(self.opts.deployer_state, name)

    def tick_state(self) -> str:
        """What the deployer did with this landing, from its own markers.

        `hold_sha` non-empty: an apply failed and the tick is holding. `behind_since`
        non-empty: the tick did not cross origin (a newer merge whose CI is still running,
        most often). Both are written by the deployer after main() returns.
        """
        if self.state("hold_sha"):
            return "held"
        if self.state("behind_since"):
            return "behind"
        return "converged"
