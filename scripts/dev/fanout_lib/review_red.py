"""The red phase of a `review.Pipeline`, and the green gate that checks the PR against it.

Split from `review.py` at that module's length cap, so the red/green measures the fan-out
adds next have room. `Pipeline` inherits these methods; each is typed against `Pipeline`
because it reads the pipeline's state and calls its `_claude`, `_git` and argv builders.
`review.py`'s docstring has the phase order and why the red phase runs where it does.
"""

from typing import TYPE_CHECKING

# Reach the sibling package: a directly-invoked script gets only its own directory on
# sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from fanout_lib.base_check import BaseCheck
from fanout_lib.git_state import GitState, changed, snapshot
from fanout_lib.hunk_check import HunkCheck
from fanout_lib.processes import reaping
from fanout_lib.red_gate import Gate, ResetFailed, red_prompt, reset_worktree

if TYPE_CHECKING:
    from fanout_lib.review import Pipeline


class RedPhase:
    """The red-phase methods `review.Pipeline` inherits."""

    def _first_implementer(self: "Pipeline") -> list[str]:
        """The implement phase's argv: on held settings when a red phase ran before it.

        The red author could leave an ignored settings file or a skip-worktree hook edit,
        neither in the range the red gate reads (#3846). `reset_worktree` removes them; held
        settings are the second layer, and drop `CLAUDE.md`, so it comes as text.
        """
        if not self.red_green:
            return self._implementer()
        return self._implementer(self._held_settings(), self._red_claude_md())

    def _red_claude_md(self: "Pipeline") -> str:
        """The start-time `CLAUDE.md` a red batch's implementer session gets as text.

        That session never loaded the project source, so its transcript holds no `CLAUDE.md`,
        and `--resume` keeps no appended system prompt: every resumed phase passes it again.
        """
        if not (self.red_green and self.project_claude_md):
            return ""
        return (
            "\n\n# The repo's CLAUDE.md, as it stood before the red phase ran\n\n"
            + self.project_claude_md
        )

    def _red(self: "Pipeline", issues: str) -> tuple[str, Gate] | None:
        """Run the test author and the red gate: the red SHA and its verdict, or None.

        Before the gate, what the red session left running is killed and the worktree reset
        to `red`, so the verdict is `red`'s tree alone (#3871): an ignored root `conftest.py`,
        a `.pth` in `.venv/` or a skip-worktree edit to the code would each make the tests
        fail for a reason no diff shows. After it, the same again, to the commit the
        implementer starts from, the base on a refusal (#3852). A failed reset raises
        `ResetFailed`, which `run_all` turns into a failed batch.
        """
        base = self._git("rev-parse", "HEAD")
        held = snapshot(self.run, self.worktree)
        with reaping():
            phase = self._claude(
                "red", self._red_author(), red_prompt(issues, self.anti_patterns)
            )
        # Before any git call reads the worktree's `.git` pointer or the shared config.
        self._refuse_git_changes(held)
        red = self._git("rev-parse", "HEAD")
        if phase.failed:
            gate = Gate(
                f"the test author's session failed ({phase.report.get('subtype') or 'error'})"
            )
        else:
            reset_worktree(self.run, self.worktree, red)
            # The gate runs the red author's tests, which could start processes too.
            with reaping():
                gate = self.gates.red(self.run, self.worktree, base, red)
            self._refuse_git_changes(held)
        reset_worktree(self.run, self.worktree, red if gate.passed else base)
        # The Stop hook reads the brief, and the red author could edit it (#3884).
        (self.worktree / ".fanout" / "brief.md").write_text(self.brief)
        out = phase.report.get("structured_output")
        behaviours = out.get("behaviours") if isinstance(out, dict) else None
        self.record.red_behaviours = (
            len(behaviours) if isinstance(behaviours, list) else 0
        )
        self.record.red_tests = len(gate.nodes)
        self.record.red_by_absence = len(gate.absent)
        self.record.red_gate = "passed" if gate.passed else gate.reason
        return (red, gate) if gate.passed else None

    def _refuse_git_changes(self: "Pipeline", held: GitState) -> None:
        """Fail the batch when git state outside the worktree moved (#3864, #3879)."""
        moved = changed(held, snapshot(self.run, self.worktree))
        if moved:
            raise ResetFailed(
                f"the red phase changed git state outside the worktree: {', '.join(moved)}"
            )

    def _unproven(
        self: "Pipeline", base: str, head: str, red: tuple[str, Gate] | None
    ) -> list[str]:
        """Record which of the PR's new tests pass without its code changes, and name them.

        Every batch of this repo runs it, red phase or not; the red nodes are left out, since
        the red gate already proved they fail there. Another repo's batch runs none, because
        the check runs this repo's pytest configuration. It only records, so any exception
        it raises lands in `base_error` and the batch goes on to its review.
        """
        if not self.target.is_server:
            return []
        try:
            check = self.base_check(
                self.run, self.worktree, base, head, red[1].nodes if red else ()
            )
        # A measure never stops the batch, so this catches what the check did not expect.
        except Exception as exc:
            check = BaseCheck(error=f"{type(exc).__name__}: {exc}")
        self.record.base_tests = check.new
        self.record.base_passing = check.passing
        self.record.base_error = check.error
        return check.passing

    def _detection(self: "Pipeline", red: tuple[str, Gate]) -> None:
        """Record which fix hunks the red tests notice when each is reverted on its own.

        Run once, on the HEAD that passed the last green gate, which is the PR that ships. The
        diff starts at the merge base, not the red commit: a branch that merged master would
        otherwise count master's hunks as fix hunks the red tests missed. The red commit
        touches only tests, which the check leaves out. It only records, so any exception it
        raises lands in `red_hunks_error`.
        """
        start = self._git("merge-base", "HEAD", self.target.base)
        try:
            check = self.hunk_check(self.run, self.worktree, start, red[1])
        # A measure never stops the batch, so this catches what the check did not expect.
        except Exception as exc:
            check = HunkCheck(error=f"{type(exc).__name__}: {exc}")
        self.record.red_hunks = check.hunks
        self.record.red_hunks_missed = check.missed
        self.record.red_hunks_by_absence = check.by_absence
        self.record.red_hunks_error = check.error

    def _green(self: "Pipeline", red: tuple[str, Gate] | None) -> str:
        """Run the green gate on HEAD and record it; "" when it passed or there is no red."""
        if red is None:
            return ""
        reason = self.gates.green(self.run, self.worktree, *red)
        self.record.green_gate = reason or "passed"
        return reason
