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
3. fix — only when a finding passes `actionable`. It resumes the implementer session. A red
   batch whose PR fails `red_gate.green_gate` gets that failure as one more finding, and the
   gate runs again after the fix; a batch still failing it is not landed.
4. delta review — a fresh reviewer reads only the fix's commits.
5. land — on the deploy host, the implementer session is resumed with the brief's Landing
   section. Elsewhere, a session resumes only to file what is left.

DECIDED: a refused red commit does not stop the batch. The refusal is what the red phase
measures, a vacuous test caught before it shipped, and the issue still deserves its fix. The
plain implementer then writes its own tests as any batch does, and the record says the red
phase was refused.

DECIDED: the fix round resumes the implementer session rather than starting a fresh one. The
implementer already holds the change's context, and the delta review is the independent check
on the fix. A fresh fixer would re-read the whole change for no extra separation.

THE STOP HOOK. `.claude/hooks/fanout-stop.py` fires in every session under the worktree. The
pipeline writes the running phase to `.fanout/phase` and resets the hook's block counter before
each call. The hook never blocks a `review` phase, whose final message is JSON, and holds a
`land` phase to a `VERDICT:` line.

WHAT THE IMPLEMENTER CAN WRITE. The worktree, and for another repo's batch the `.fanout/server`
snapshot this module runs from. The pipeline therefore reads the review prompt, the headless
system prompt, this repo's `.claude/settings.json` and every hook once, at start (#3763,
#3794, #3810). Each phase gets the prompts as text and runs the Stop hook from a copy outside
the worktree that is rewritten before every call. A phase that resumes the implementer's
session also loads no settings file from the worktree; `held_hooks` says which phases and why.

DISCLOSURE. The repo is public. A finding in category `security` reaches the PR comment as a
count only, is never filed with `findings.py open`, and is kept in full only in the local
record under `STATE_DIR`.

The local record also carries each phase's cost and the finding counts. It is how slice 1's
kill criterion is measured, and it outlives the worktree that `clean` removes.
"""

import json
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

# Reach the sibling package: a directly-invoked script gets only its own directory on
# sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys

_sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fanout_lib.brief import ISSUES_HEADING, _landing, lands
from fanout_lib.held_hooks import pointed_settings, read_hooks, write_hooks
from fanout_lib.review_prompts import (
    FINDINGS_SCHEMA,
    _as_data,
    delta_prompt,
    fix_prompt,
    review_prompt,
)
from fanout_lib.red_gate import (
    GREEN_FILE,
    RED_SCHEMA,
    Gate,
    Gates,
    anti_patterns,
    green_finding,
    red_prompt,
    red_section,
    stray_config,
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
SOURCE_ROOT = Path(__file__).resolve().parents[3]
HEADLESS_PROMPT_FILE = SOURCE_ROOT / SYSTEM_PROMPT_FILE
# Names the pipeline's own copy of `fanout-stop.py`. The worktree's copy, which this repo's
# `.claude/settings.json` registers too, stands down while the file exists, so one Stop spends
# the block cap once. `.claude/hooks/fanout-stop.py` mirrors the path.
OWN_COPY = Path(".fanout") / "stop-hook"
REVIEW_BUDGET_USD = 15
# A finding the fix round acts on. The reviewer reports everything, as the user-level
# `## Code review` rule asks; this is the separate filtering pass.
ACTIONABLE_SEVERITIES = frozenset({"critical", "high", "medium"})
CONFIDENCE_FLOOR = 0.6
# `land.sh` waits up to about an hour for CI and the tick. A land phase started with less than
# this left on the unit's `RuntimeMaxSec` would be killed mid-landing, so it is skipped.
LAND_MARGIN_S = 90 * 60
GATES = Gates()
STATE_DIR = Path.home() / ".local" / "state" / "fanout-review"

# One process boundary for claude, git and gh: argv and stdin in, the finished process out.
Runner = Callable[[list[str], str | None], subprocess.CompletedProcess]


def run_process(argv: list[str], stdin: str | None) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, input=stdin, capture_output=True, text=True, check=False
    )


@dataclass
class Phase:
    """One `claude -p` call: its name and the JSON report it printed."""

    name: str
    report: dict

    @property
    def text(self) -> str:
        return str(self.report.get("result") or "")

    @property
    def cost(self) -> float:
        return float(self.report.get("total_cost_usd") or 0)

    @property
    def failed(self) -> bool:
        return bool(self.report.get("is_error"))


@dataclass
class Record:
    """What the local record under `STATE_DIR` and the PR comment are written from."""

    batch: str
    pr: str = ""
    costs: dict[str, float] = field(default_factory=dict)
    review_error: str = ""
    findings: list[dict] = field(default_factory=list)
    actionable: list[dict] = field(default_factory=list)
    remaining: list[dict] = field(default_factory=list)
    # The red/green measure (#3674): "" when the batch had no red phase, else "passed" or the
    # gate's reason. A refused red gate is a vacuous test caught.
    red_gate: str = ""
    red_behaviours: int = 0
    red_tests: int = 0
    green_gate: str = ""


def actionable(findings: Sequence[dict]) -> list[dict]:
    """The findings the fix round acts on: medium or worse, at or above the confidence floor."""
    return [
        f
        for f in findings
        if f.get("severity") in ACTIONABLE_SEVERITIES
        and float(f.get("confidence") or 0) >= CONFIDENCE_FLOOR
    ]


def is_held(finding: dict) -> bool:
    """Whether a finding stays off the public PR and tracker for disclosure reasons."""
    return finding.get("category") == "security"


def findings_of(phase: Phase) -> tuple[list[dict] | None, str]:
    """The reviewer's findings, or None and the reason the review produced none."""
    if phase.failed:
        return (
            None,
            f"the review session failed ({phase.report.get('subtype') or 'error'})",
        )
    out = phase.report.get("structured_output")
    if not isinstance(out, dict) or not isinstance(out.get("findings"), list):
        return None, "the review session returned no structured findings"
    return [f for f in out["findings"] if isinstance(f, dict)], ""


def issues_section(brief: str) -> str:
    """The brief's fenced issue text, which is all the reviewer learns about intent."""
    _, sep, rest = brief.partition(ISSUES_HEADING)
    return sep + rest if sep else ""


def land_prompt(record: Record, landing: str) -> str:
    public = [f for f in record.remaining if not is_held(f)]
    if record.review_error:
        state = f"The review did not complete: {record.review_error}. Land without it, and say so."
    elif public:
        state = (
            "These findings were not resolved. File each with `findings.py open` before you "
            "land, and name it in the PR body as `Filed for later: #N`.\n\n"
            + _as_data("The unresolved findings", public)
        )
    else:
        state = "No finding is left to file."
    return f"""The review of {record.pr} is finished. {state}

Now land the PR. Your brief's own Landing section said to stop at the PR; this replaces it:

{landing}
End your final message with the PR URL and quote `land.sh`'s `VERDICT:` line.
"""


def file_prompt(record: Record) -> str:
    public = [f for f in record.remaining if not is_held(f)]
    return f"""The review of {record.pr} is finished. These findings were not resolved. File
each with `findings.py open`, and add `Filed for later: #N` to the PR body with `gh pr edit`.
Do not merge or land.

{_as_data("The unresolved findings", public)}

End your final message with the PR URL.
"""


def comment_body(record: Record) -> str:
    """The PR comment: the public trail of the review, its counts and its costs."""
    lines = ["## Fan-out review", ""]
    if record.review_error:
        lines += [f"The review did not complete: {record.review_error}.", ""]
    if record.red_gate == "passed":
        lines.append(
            f"Red gate passed: {record.red_tests} tests for {record.red_behaviours} stated "
            "behaviours failed on the unchanged code."
        )
    elif record.red_gate:
        lines.append(f"Red gate refused the test author's commit: {record.red_gate}.")
    if record.green_gate:
        lines.append(f"Green gate: {record.green_gate}.")
    if record.red_gate:
        lines.append("")
    held = sum(1 for f in record.findings if is_held(f))
    lines.append(
        f"{len(record.findings)} findings, {len(record.actionable)} actionable "
        f"(severity medium or worse, confidence {CONFIDENCE_FLOOR} or more), "
        f"{len(record.remaining)} left after the fix round."
    )
    if held:
        lines.append(
            f"{held} security findings are held off this public page and kept in the "
            "orchestrator's local record."
        )
    shown = [f for f in record.findings if not is_held(f)]
    if shown:
        lines += [
            "",
            "| Severity | Confidence | Where | Finding |",
            "|---|---|---|---|",
        ]
        for f in shown:
            where = f"`{f.get('file', '')}:{f.get('line', '')}`".replace("|", "\\|")
            title = str(f.get("title", "")).replace("|", "\\|").replace("\n", " ")
            lines.append(
                f"| {f.get('severity')} | {f.get('confidence')} | {where} | {title} |"
            )
    costs = ", ".join(f"{k} ${v:.2f}" for k, v in record.costs.items())
    lines += ["", f"Cost by phase: {costs}."]
    return "\n".join(lines) + "\n"


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
        self.anti_patterns = anti_patterns() if red_green else ""
        self.record = Record(batch)
        self.session = ""
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
        self.hook_root = state_dir / f"{batch}-stop-hook"

    def _claude(self, name: str, argv: list[str], stdin: str) -> Phase:
        fanout = self.worktree / ".fanout"
        (self.worktree / OWN_COPY).write_text(
            f"{write_hooks(self.hooks, self.hook_root)}\n"
        )
        (fanout / "phase").write_text(
            f"{'review' if name.startswith('review') else name}\n"
        )
        (fanout / "stop-blocks").write_text("0\n")
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
        self.record.costs[name] = self.record.costs.get(name, 0) + phase.cost
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
        """The prefix a resumed phase runs `claude` with: no project or local settings file.

        For this repo's batch it passes the project settings read at start, each hook command
        pointed at the copy in `hook_root`. That set already registers `fanout-stop`, so the
        Stop hook is not added a second time. Another repo's batch keeps `_stop_hook`'s.
        """
        if not self.project_settings:
            return ["claude", "--setting-sources", "user", *self._stop_hook()[1:]]
        settings = pointed_settings(self.project_settings, self.hook_root)
        return [
            "claude", "--setting-sources", "user", "--settings", json.dumps(settings),
        ]  # fmt: skip

    def _implementer(self, prefix: list[str] | None = None) -> list[str]:
        return [
            *(prefix or self._stop_hook()),
            "-p", "--model", "opus", "--permission-mode", "auto",
            "--output-format", "json", "--max-budget-usd", str(BUDGET_USD),
            "--append-system-prompt", self.headless_prompt,
        ]  # fmt: skip

    def _red_author(self) -> list[str]:
        return [
            *self._stop_hook(),
            "-p", "--model", "opus", "--permission-mode", "auto",
            "--output-format", "json", "--max-budget-usd", str(REVIEW_BUDGET_USD),
            "--json-schema", json.dumps(RED_SCHEMA),
        ]  # fmt: skip

    def _red(self, issues: str) -> tuple[str, Gate] | None:
        """Run the test author and the red gate: the red SHA and its verdict, or None.

        A refused commit is reset away, so the implementer starts from the base as usual.
        """
        base = self._git("rev-parse", "HEAD")
        phase = self._claude(
            "red", self._red_author(), red_prompt(issues, self.anti_patterns)
        )
        red = self._git("rev-parse", "HEAD")
        if phase.failed:
            gate = Gate(
                f"the test author's session failed ({phase.report.get('subtype') or 'error'})"
            )
        else:
            gate = self.gates.red(self.run, self.worktree, base, red)
        out = phase.report.get("structured_output")
        behaviours = out.get("behaviours") if isinstance(out, dict) else None
        self.record.red_behaviours = (
            len(behaviours) if isinstance(behaviours, list) else 0
        )
        self.record.red_tests = len(gate.nodes)
        self.record.red_gate = "passed" if gate.passed else gate.reason
        if gate.passed:
            return red, gate
        self._git("reset", "--hard", base)
        self._git("clean", "-fd")
        # `clean` without `-x` keeps ignored files, and a root conftest.py is one.
        for stray in stray_config(self.run, self.worktree):
            (self.worktree / stray).unlink(missing_ok=True)
        return None

    def _green(self, red: tuple[str, Gate] | None) -> str:
        """Run the green gate on HEAD and record it; "" when it passed or there is no red."""
        if red is None:
            return ""
        reason = self.gates.green(self.run, self.worktree, *red)
        self.record.green_gate = reason or "passed"
        return reason

    def _resume(self) -> list[str]:
        return [*self._implementer(self._held_settings()), "--resume", self.session]

    def _reviewer(self) -> list[str]:
        return [
            *self._stop_hook(),
            "-p", "--model", "opus", "--permission-mode", "auto",
            "--output-format", "json", "--max-budget-usd", str(REVIEW_BUDGET_USD),
            "--append-system-prompt", self.review_prompt,
            "--disallowedTools", "Edit,Write,NotebookEdit",
            "--json-schema", json.dumps(FINDINGS_SCHEMA),
        ]  # fmt: skip

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
        red = self._red(issues) if self.red_green else None
        brief = self.brief
        if red is not None:
            brief = brief.replace(ISSUES_HEADING, red_section(*red) + ISSUES_HEADING, 1)
        impl = self._claude("implement", self._implementer(), brief)
        pr = PR_URL.search(impl.text)
        if impl.failed or not pr:
            if self.red_green:
                self._save()
            return impl.report
        self.record.pr = pr.group(0)
        self.session = str(impl.report.get("session_id") or "")
        base = self._git("merge-base", "HEAD", self.target.base)
        head = self._git("rev-parse", "HEAD")

        review = self._claude(
            "review", self._reviewer(), review_prompt(issues, base, head)
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
                    delta_prompt(issues, head, after, self.record.actionable, fix.text),
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

        self._comment()
        final = self._held_for_green(green) if green else self._finish(last)
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
            landing = _landing(self.host, self.batch, self.target)
            return self._claude(
                "land", self._resume(), land_prompt(self.record, landing)
            ).report
        if any(not is_held(f) for f in self.record.remaining) and self.session:
            return self._claude("file", self._resume(), file_prompt(self.record)).report
        return last.report
