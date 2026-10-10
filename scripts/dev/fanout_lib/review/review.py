"""The review pipeline a `launch --review` batch runs in place of a single `claude -p`.

WHY. A fan-out batch used to implement, test, open its PR and land it in one context, and no
other context ever read the change (`docs/failure-classes.md` class 1, "Findings get an
adversarial pass; fixes get none"). This module puts a separate review between the PR and the
landing. The lint and test checks that finish in seconds stay with the implementer; only the
slow model review moves here.

THE PHASES, each one `claude -p` in the batch's worktree:

0. red — only for a batch whose issues carry `red_gate.RED_GREEN_LABEL`. A fresh session gets
   the issue text and nothing else, writes one failing test per stated behaviour and commits
   it. `red_gate.red_gate` then proves the tests fail on the unchanged code; the module says
   how. A refused red commit is reset away and the batch runs as if it had no red phase.
1. implement — the brief on stdin. A `--review` brief tells the agent to stop at the PR.
2. review — a fresh session with the issue text and the diff only, never the implementer's
   transcript. Edit and Write are denied to it, and it returns findings as structured output.
   Before it, this repo's batch runs `base_check`, and the reviewer is told which of the PR's
   new tests pass with its code changes taken out.
3. fix — only when a finding passes `actionable`. It resumes the implementer session. A red
   batch whose PR fails `red_gate.green_gate` gets that failure as one more finding, and the
   gate runs again after the fix; a batch still failing it is not landed. A batch passing it
   has each fix hunk reverted on its own under the red tests (`hunk_check`), for the record.
4. delta review — a fresh reviewer reads only the fix's commits.
5. land — on the deploy host, the pipeline runs `land.sh` itself (`review_land`) and resumes
   the implementer only for a verdict that needs a decision, as the `apply` phase. Before the
   landing, and on every other host instead of it, a session resumes to file what is left.

DECIDED: a refused red commit does not stop the batch. The refusal means the test author's
tests proved nothing, not that the issue is wrong, and the issue still deserves its fix. The
plain implementer then writes its own tests as any batch does, and the record says the red
phase was refused.

DECIDED: the fix round resumes the implementer session rather than starting a fresh one. The
implementer already holds the change's context, and the delta review is the independent check
on the fix. A fresh fixer would re-read the whole change for no extra separation.

THE STOP HOOK. `.claude/hooks/fanout-stop.py` fires in every session under the worktree. The
pipeline writes the running phase to `.fanout/phase` and resets the hook's block counter before
each call. The hook never blocks a `review` phase, whose final message is JSON. Every other
phase owes a PR URL; the landing's `VERDICT:` line is in its log, which the pipeline wrote.

WHAT THE IMPLEMENTER CAN WRITE. The worktree, and for another repo's batch the `.fanout/server`
snapshot this module runs from. The pipeline therefore reads the review prompt, the headless
system prompt, this repo's `.claude/settings.json` and every hook once, at start (#3763,
#3794, #3810). Each phase gets the prompts as text and runs the Stop hook from a copy outside
the worktree that is rewritten before every call. In this repo's batch, every phase after the
implementer also loads no settings file from the worktree, and the reviewer gets the start-time
`CLAUDE.md` as text; `held_hooks` says why. A red batch's implement phase runs the same way,
because the red author had the worktree before it (#3846). Both before and after the red
gate, the pipeline kills whatever the red phase left running (`processes.reaping`) and runs
`worktree_reset.reset_worktree` (#3852, #3871); `Pipeline._red` says why.

DISCLOSURE. The repo is public. A finding in category `security` reaches the PR comment as a
count only, is never filed with `findings.py open`, and is kept in full only in the local
record under `STATE_DIR`.

The local record also carries each phase's cost, the finding counts and the red/green
measures, and it outlives the worktree that `clean` removes. `scripts/dev/fanout_review_stats.py`
sums the records into the measures that decide whether the red phase stays.
"""

import json
import subprocess
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

# Reach the sibling package: a directly-invoked script gets only its own directory on
# sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys

_sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fanout_lib.brief import ISSUES_HEADING, lands
from fanout_lib.review.held_hooks import (
    held_secret_paths,
    pointed_settings,
    read_hooks,
    write_hooks,
)
from fanout_lib.review.review_prompts import (
    FINDINGS_SCHEMA,
    delta_prompt,
    file_prompt,
    fix_prompt,
    is_held,
    apply_prompt,
    review_prompt,
)
from fanout_lib.review.red_gate import (
    GREEN_FILE,
    RED_SCHEMA,
    Gate,
    Gates,
    anti_patterns,
    green_cause,
    green_finding,
    red_prompt,
    red_section,
    labelled_skip_reason,
)
from fanout_lib.review.hardened_runs import ResetFailed
from fanout_lib.review.worktree_reset import reset_worktree
from fanout_lib.review.base_check import BaseCheck, unproven_tests
from fanout_lib.review.git_state import GitState, changed, snapshot
from fanout_lib.review.hunk_check import HunkCheck, red_detection
from fanout_lib.review.processes import reaping, run_process
from fanout_lib.review.review_land import RESUME_VERDICTS, land
from fanout_lib.review.review_land import report as landing_report
from fanout_lib.review.review_record import (
    Phase,
    Record,
    actionable,
    blocking,
    comment_body,
    findings_of,
    issues_section,
    outcome,
)
from fanout_lib.launch import (
    BUDGET_USD,
    REVIEW_RUNTIME_MAX_S,
    SYSTEM_PROMPT_FILE,
    stop_hook_settings,
)
from fanout_lib.status import PR_URL
from fanout_lib.target import Target

PROMPT_FILE = Path(__file__).resolve().parent / "review_system_prompt.md"
# The checkout this module was loaded from: the batch worktree for this repo, the batch's
# `.fanout/server` snapshot for another repo. Both are trees the implementer can write.
SOURCE_ROOT = Path(__file__).resolve().parents[4]
HEADLESS_PROMPT_FILE = SOURCE_ROOT / SYSTEM_PROMPT_FILE
# Names the pipeline's own copy of `fanout-stop.py`. The worktree's copy, which this repo's
# `.claude/settings.json` registers too, stands down while the file exists, so one Stop spends
# the block cap once. `.claude/hooks/fanout-stop.py` mirrors the path.
OWN_COPY = Path(".fanout") / "stop-hook"
REVIEW_BUDGET_USD = 15
# A finding the fix round acts on. The reviewer reports everything, as the user-level
# `## Code review` rule asks; this is the separate filtering pass.
# `land.sh` waits up to about an hour for CI and the tick. A landing started with less than
# this left on the unit's `RuntimeMaxSec` would outlive the wait on it, so it is skipped.
LAND_MARGIN_S = 90 * 60
GATES = Gates()
STATE_DIR = Path.home() / ".local" / "state" / "fanout-review"

# One process boundary for claude, git and gh: argv and stdin in, the finished process out.
Runner = Callable[[list[str], str | None], subprocess.CompletedProcess]


# DECIDED: one module over the 600-line guideline rather than a mixin. The red phase and the
# green gate read the pipeline's state and call its `_claude`, `_git` and argv builders, so a
# module holding them alone could not be read without this class (#3661's `conjoined:` rule).
class Pipeline:
    """One batch's phases, run in order against one worktree.

    Args:
        worktree: the batch's worktree, the cwd of every call.
        batch: the batch id.
        host: the host this runs on, which decides whether the batch lands.
        target: the repo the batch works.
        brief: the brief text the unit fed on stdin.
        run: the process boundary; tests pass a fake.
        clock: seconds since some fixed point; tests pass a fake.
        state_dir: where the local record goes.
        red_green: run the red phase and both gates before and after the implementer.
        gates: the red and green gates; tests pass scripted ones.
        base_check: which new tests pass without the PR's code changes; tests script it.
        hunk_check: which fix hunks the red tests notice reverted; tests script it.
    """

    def __init__(
        self,
        worktree: Path,
        batch: str,
        host: str,
        target: Target,
        brief: str,
        run: Runner = run_process,
        clock: Callable[[], float] = time.monotonic,
        state_dir: Path = STATE_DIR,
        red_green: bool = False,
        gates: Gates = GATES,
        base_check: Callable[..., BaseCheck] = unproven_tests,
        hunk_check: Callable[..., HunkCheck] = red_detection,
    ):
        self.worktree = worktree
        self.batch = batch
        self.host = host
        self.target = target
        self.brief = brief
        self.run = run
        self.clock = clock
        self.deadline = clock() + REVIEW_RUNTIME_MAX_S
        self.state_dir = state_dir
        self.red_green = red_green
        self.gates = gates
        self.base_check = base_check
        self.hunk_check = hunk_check
        # Read at start for the red author and every reviewer (#4023): the skill lives outside
        # the worktree, but one read keeps every phase on the same text.
        self.anti_patterns = anti_patterns()
        self.record = Record(batch)
        self.session = ""
        # The implementer session's `total_cost_usd` so far: a resumed session reports its
        # whole-session total, so each resumed phase records only the increase (#3939).
        self.session_cost = 0.0
        # Read now, before the implementer runs. A server unit imports this module from the
        # batch worktree, which that agent can write, so a file read at review time would
        # take whatever the implementer left there. The headless prompt and the Stop hook
        # are held the same way for every phase the implementer's session resumes into
        # (#3794): each phase gets the prompt as text and runs the hook from `hook_root`,
        # which `_snapshot_hook` rewrites from these bytes before every call.
        self.review_prompt = PROMPT_FILE.read_text()
        self.headless_prompt = HEADLESS_PROMPT_FILE.read_text()
        self.hooks = read_hooks(SOURCE_ROOT)
        # Another repo's snapshot carries no `.claude/settings.json`, and its worktree's
        # settings are that repo's, not these.
        self.project_settings = (
            (SOURCE_ROOT / ".claude" / "settings.json").read_text()
            if target.is_server
            else ""
        )
        # The reviewer loads no project source, so no `CLAUDE.md`: it gets this copy as text.
        self.project_claude_md = (
            (SOURCE_ROOT / "CLAUDE.md").read_text() if target.is_server else ""
        )
        self.hook_root = state_dir / f"{batch}-stop-hook"

    def _claude(self, name: str, argv: list[str], stdin: str) -> Phase:
        fanout = self.worktree / ".fanout"
        # `reset_worktree` keeps the directory only while one of `FANOUT_KEPT` is in it.
        fanout.mkdir(exist_ok=True)
        (self.worktree / OWN_COPY).write_text(
            f"{write_hooks(self.hooks, self.hook_root)}\n"
        )
        (fanout / "phase").write_text(
            f"{'review' if name.startswith('review') else name}\n"
        )
        (fanout / "stop-blocks").write_text("0\n")
        started = self.clock()
        proc = self.run(argv, stdin)
        try:
            report = json.loads(proc.stdout)
        except json.JSONDecodeError:
            report = {}
        if not isinstance(report, dict) or not report:
            report = {
                "type": "result",
                "is_error": True,
                "subtype": f"no report (exit {proc.returncode})",
                "result": proc.stderr.strip()[-2000:],
            }
        (fanout / f"{name}.json").write_text(json.dumps(report))
        phase = Phase(name, report)
        cost = phase.cost
        if name == "implement" or "--resume" in argv:
            # A failed resume can report no total at all, which is no reason to forget the
            # session's spend so far.
            cost = max(phase.cost - self.session_cost, 0.0)
            self.session_cost = max(self.session_cost, phase.cost)
        record = self.record
        record.costs[name] = record.costs.get(name, 0) + cost
        record.durations[name] = round(
            record.durations.get(name, 0) + self.clock() - started, 1
        )
        denials = report.get("permission_denials")
        record.permission_denials[name] = record.permission_denials.get(name, 0) + (
            len(denials) if isinstance(denials, list) else 0
        )
        return phase

    def _git(self, *args: str) -> str:
        return self.run(["git", "-C", str(self.worktree), *args], None).stdout.strip()

    def _stop_hook(self) -> list[str]:
        """The prefix that runs `claude` under the Stop hook held since start.

        `launch.claude_args` names the hook by path inside a tree the agent can write, so a
        later phase would run whatever the agent left there.
        """
        settings = json.dumps(stop_hook_settings(str(self.hook_root)))
        return ["claude", "--settings", settings]

    def _held_settings(self) -> list[str]:
        """The prefix a phase after the implementer runs `claude` with.

        For this repo's batch it loads no project or local settings file, and passes the
        project settings read at start, each hook command pointed at the copy in `hook_root`.
        That set already registers `fanout-stop`, so the Stop hook is not added a second time.
        Another repo's batch keeps `_stop_hook`'s prefix and its own project settings: the
        pipeline holds no copy of that repo's hooks, so dropping the source would drop them.
        """
        if not self.project_settings:
            return self._stop_hook()
        settings = pointed_settings(self.project_settings, self.hook_root)
        return [
            "claude", "--setting-sources", "user", "--settings", json.dumps(settings),
        ]  # fmt: skip

    def _implementer(
        self, prefix: list[str] | None = None, prompt: str = ""
    ) -> list[str]:
        return [
            *(prefix or self._stop_hook()),
            "-p", "--model", "opus", "--permission-mode", "auto",
            "--output-format", "json", "--max-budget-usd", str(BUDGET_USD),
            "--append-system-prompt", self.headless_prompt + prompt,
        ]  # fmt: skip

    def _red_author(self) -> list[str]:
        return [
            *self._stop_hook(),
            "-p", "--model", "opus", "--permission-mode", "auto",
            "--output-format", "json", "--max-budget-usd", str(REVIEW_BUDGET_USD),
            "--json-schema", json.dumps(RED_SCHEMA),
        ]  # fmt: skip

    def _resume(self) -> list[str]:
        held = self._implementer(self._held_settings(), self._red_claude_md())
        return [*held, "--resume", self.session]

    def _reviewer(self) -> list[str]:
        prompt = self.review_prompt
        if self.anti_patterns:
            prompt += "\n\n# What a vacuous test looks like\n\n" + self.anti_patterns
        if self.project_claude_md:
            prompt += (
                "\n\n# The repo's CLAUDE.md, as it stood before the implementer ran\n\n"
                + self.project_claude_md
            )
        return [
            *self._held_settings(),
            "-p", "--model", "opus", "--permission-mode", "auto",
            "--output-format", "json", "--max-budget-usd", str(REVIEW_BUDGET_USD),
            "--append-system-prompt", prompt,
            "--disallowedTools", "Edit,Write,NotebookEdit",
            "--json-schema", json.dumps(FINDINGS_SCHEMA),
        ]  # fmt: skip

    def _first_implementer(self) -> list[str]:
        """The implement phase's argv: on held settings when a red phase ran before it.

        The red author could leave an ignored settings file or a skip-worktree hook edit,
        neither in the range the red gate reads (#3846). `reset_worktree` removes them; held
        settings are the second layer, and drop `CLAUDE.md`, so it comes as text.
        """
        if not self.red_green:
            return self._implementer()
        return self._implementer(self._held_settings(), self._red_claude_md())

    def _red_claude_md(self) -> str:
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

    def _red(self, issues: str) -> tuple[str, Gate] | None:
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

    def _refuse_git_changes(self, held: GitState) -> None:
        """Fail the batch when git state outside the worktree moved (#3864, #3879)."""
        moved = changed(held, snapshot(self.run, self.worktree))
        if moved:
            raise ResetFailed(
                f"the red phase changed git state outside the worktree: {', '.join(moved)}"
            )

    def _unproven(
        self, base: str, head: str, red: tuple[str, Gate] | None
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

    def _detection(self, red: tuple[str, Gate]) -> None:
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

    def _green(self, red: tuple[str, Gate] | None) -> str:
        """Run the green gate on HEAD and record it; "" when it passed or there is no red."""
        if red is None:
            return ""
        reason = self.gates.green(self.run, self.worktree, *red)
        self.record.green_gate = reason or "passed"
        self.record.green_first = self.record.green_first or green_cause(reason)
        return reason

    def _save(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path = self.state_dir / f"{self.batch}-{stamp}.json"
        path.write_text(json.dumps(asdict(self.record), indent=2))

    def run_all(self) -> dict:
        """Run every phase and return the report the unit writes to `report.json`.

        That report is the last phase's, so `status` reads it exactly as it reads a
        single-session batch: a PR URL, a `VERDICT:` line, or a blocker line.
        """
        issues = issues_section(self.brief)
        if self.project_settings:
            self.hooks.update(held_secret_paths(self.run, SOURCE_ROOT))
        if not self.red_green:
            self.record.red_skipped = labelled_skip_reason(
                self.run, self.batch, self.target.is_server
            )
        try:
            red = self._red(issues) if self.red_green else None
        except ResetFailed as exc:
            self.record.red_gate = f"reset failed: {exc}"
            self.record.outcome = "failed"
            self._save()
            return {"type": "result", "is_error": True, "result": f"failed: {exc}"}
        brief = self.brief
        if red is not None:
            brief = brief.replace(ISSUES_HEADING, red_section(*red) + ISSUES_HEADING, 1)
        impl = self._claude("implement", self._first_implementer(), brief)
        pr = PR_URL.search(impl.text)
        if impl.failed or not pr:
            # Saved whether or not the batch is red-green: a batch that never reached a PR is
            # the failure rate the records exist to measure (#3940).
            self.record.outcome = outcome(impl.report, has_pr=False)
            self._save()
            return impl.report
        self.record.pr = pr.group(0)
        self.session = str(impl.report.get("session_id") or "")
        base = self._git("merge-base", "HEAD", self.target.base)
        head = self._git("rev-parse", "HEAD")

        passing = self._unproven(base, head, red)
        review = self._claude(
            "review",
            self._reviewer(),
            review_prompt(issues, base, head, red[1].absent if red else (), passing),
        )
        found, error = findings_of(review)
        self.record.review_error = error
        self.record.findings = found or []
        green = self._green(red)
        if green and red is not None:
            self.record.findings.append(green_finding(green, *red))
        self.record.actionable = actionable(self.record.findings)
        last = impl
        if self.record.actionable and self.session:
            fix = self._claude(
                "fix",
                self._resume(),
                fix_prompt(
                    self.record.actionable, self.record.pr, red[0] if red else ""
                ),
            )
            last = fix if PR_URL.search(fix.text) else impl
            after = self._git("rev-parse", "HEAD")
            if after == head:
                self.record.remaining = list(self.record.actionable)
            else:
                delta = self._claude(
                    "review-delta",
                    self._reviewer(),
                    delta_prompt(
                        issues, base, head, after, self.record.actionable, fix.text
                    ),
                )
                left, delta_error = findings_of(delta)
                # A failed delta review proves nothing was resolved.
                self.record.remaining = (
                    actionable(left)
                    if left is not None
                    else list(self.record.actionable)
                )
                if delta_error:
                    self.record.review_error = f"delta review: {delta_error}"
            # The fix may touch the red tests even when the first run passed, so the PR that
            # ships is the one the gate reads.
            if red is not None:
                green = self._green(red)
                if not green:
                    self.record.remaining = [
                        f for f in self.record.remaining if f.get("file") != GREEN_FILE
                    ]

        if red is not None and not green:
            self._detection(red)
        self._comment()
        if green:
            final = self._held_for_green(green)
        else:
            final = self._held_for_findings() or self._finish(last)
        self.record.outcome = outcome(final, has_pr=True)
        self._save()
        held = sum(1 for f in self.record.remaining if is_held(f))
        if held:
            final = dict(final)
            final["result"] = (
                f"{held} unresolved security findings are held off the public tracker; "
                f"read them in {self.state_dir}.\n{final.get('result') or ''}"
            )
        return final

    def _comment(self) -> None:
        self.run(
            ["gh", "pr", "comment", self.record.pr, "--body-file", "-"],
            comment_body(self.record),
        )

    def _held_for_findings(self) -> dict | None:
        """A `needs input:` report holding the open PR on a confident leftover, else None.

        On every host: a daniel-server batch's PR is landed by the orchestrator from `status`'s
        `done` line, which would land it just the same. A security finding is counted rather
        than named, because this text reaches `status` and the orchestrator's transcript.
        """
        left = blocking(self.record.remaining)
        if not left:
            return None
        named = [f"{f.get('title')} ({f.get('file')})" for f in left if not is_held(f)]
        held = len(left) - len(named)
        if held:
            named.append(f"{held} security findings in {self.state_dir}")
        return {
            "type": "result",
            "is_error": False,
            "result": (
                "needs input: the review left a confident medium-or-worse finding after the "
                f"fix round, so the PR was not landed: {'; '.join(named)}.\n{self.record.pr}"
            ),
        }

    def _held_for_green(self, reason: str) -> dict:
        return {
            "type": "result",
            "is_error": False,
            "result": (
                f"needs input: the PR fails the green gate, so it was not landed: {reason}."
                f"\n{self.record.pr}"
            ),
        }

    def _finish(self, last: Phase) -> dict:
        if lands(self.host, self.target.repo):
            if self.deadline - self.clock() < LAND_MARGIN_S:
                return {
                    "type": "result",
                    "is_error": False,
                    "result": (
                        "needs input: the review phases left too little of the unit's run "
                        f"time to land safely; land {self.record.pr} by hand.\n{self.record.pr}"
                    ),
                }
            # Filed before the merge, so the PR body names each leftover's issue.
            self._file_leftovers()
            landing = land(
                self.run,
                self.worktree,
                self.record.pr,
                lambda: self.deadline - self.clock(),
            )
            self.record.verdict = landing.line or f"none (exit {landing.rc})"
            if landing.verdict in RESUME_VERDICTS and self.session:
                prompt = apply_prompt(
                    self.record.pr,
                    landing.line,
                    landing.tail,
                    str(self.worktree / ".fanout"),
                )
                return self._claude("apply", self._resume(), prompt).report
            return landing_report(self.record.pr, landing)
        return self._file_leftovers() or last.report

    def _file_leftovers(self) -> dict | None:
        """Resume the implementer to file the public leftovers; its report, or None if none."""
        if any(not is_held(f) for f in self.record.remaining) and self.session:
            return self._claude("file", self._resume(), file_prompt(self.record)).report
        return None
