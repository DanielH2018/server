"""What one `--review` batch records: its phases, the findings, and the PR comment built from them.

Split from `review.py`, which runs the phases, so the record's shape and its rendering can grow
without that module crossing its length cap. `review.py` re-exports every name here.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field

# Reach the sibling package: a directly-invoked script gets only its own directory on
# sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from fanout_lib.brief import ISSUES_HEADING
from fanout_lib.review.green_contest import judgment
from fanout_lib.review.review_prompts import is_held
from fanout_lib.status import BLOCKER, NO_PR

ACTIONABLE_SEVERITIES = frozenset({"critical", "high", "medium"})
# DECIDED: 0.5, not 0.6 (#3675). Judged against 86 batches' records, 5 of the 6 medium findings
# at 0.5-0.55 were material defects the fix round never saw; at 0.45 it was 3 of 7.
CONFIDENCE_FLOOR = 0.5
REVIEW_EFFORT = "medium"
HOLD_CONFIDENCE = 0.8
# The `test` findings the fix round takes at any severity: a test that checks nothing. The
# severity floor dropped 46 of the reviewer's 50 test findings, these among them (#4023).
# `scaffold` was acted on too until #3675 judged it: 1 of 7 actionable scaffold findings was
# real, the rest mostly a duplicate of a stronger test, while all 9 vacuous ones were real. A
# scaffold or missing-coverage finding keeps the severity floor.
ACTED_ON_TEST_SUBKINDS = frozenset({"vacuous"})


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
    # The `--effort` every reviewer call passes, read from here so the record names what ran
    # and actionable findings per PR can be compared between settings (#3966). A record
    # written before this field existed has no key.
    review_effort: str = REVIEW_EFFORT
    costs: dict[str, float] = field(default_factory=dict)
    review_error: str = ""
    findings: list[dict] = field(default_factory=list)
    actionable: list[dict] = field(default_factory=list)
    remaining: list[dict] = field(default_factory=list)
    # The red gate's verdict (#3674): "" when the batch had no red phase, else "passed" or the
    # gate's reason. The test author reruns its tests until they fail, so a refusal is rare
    # whatever the tests are worth; `green_first` and the `red_hunks` fields measure them.
    red_gate: str = ""
    red_behaviours: int = 0
    red_tests: int = 0
    # How many red tests failed only because a name was missing (#4023).
    red_by_absence: int = 0
    green_gate: str = ""
    # The first green gate run's `red_gate.green_cause`: `passed`, `unmet`, `flaky` or `lock`.
    green_first: str = ""
    # The implementer's contest of a red test (`green_contest`, #5400): "" when there was
    # none, else `upheld: ` or `rejected: ` and the contested nodes.
    green_contest: str = ""
    # The PR's new test nodes outside the red phase, those of them that pass with its code
    # changes taken out (`base_check`), and why the check could not run, if it could not.
    base_tests: int = 0
    base_passing: list[str] = field(default_factory=list)
    base_error: str = ""
    # The fix hunks tried by reverting each alone under the red tests (`hunk_check`), those
    # no red test noticed, those noticed only through a missing name, and any error.
    red_hunks: int = 0
    red_hunks_missed: list[str] = field(default_factory=list)
    red_hunks_by_absence: int = 0
    red_hunks_error: str = ""
    # Why the batch ran no red phase (`red_gate.red_skip_reason`), "" when it ran one.
    red_skipped: str = ""
    # How the batch ended, so the records count the batches that never reached a PR (#3940):
    # `failed`, `needs-input` or `no-pr` before a PR exists, then `pr` once one does, or
    # `failed` / `needs-input` when the last phase says so.
    outcome: str = ""
    # The head the last reviewer read: the head the first review read, or the fix round's head
    # when a delta review read that (#5496). `fanout_lib.autoland` lands a PR unattended only
    # while GitHub still shows this head, so a push after the review stops at the PR. "" in a
    # record written before this field existed.
    head: str = ""
    # The VERDICT line of the landing the pipeline ran (#3960), "" when it ran none.
    verdict: str = ""
    # The CI wait a batch that does not land runs (#4737): "" when it ran none, else its
    # result in words, and whether the implementer was resumed for a red verdict.
    ci_gate: str = ""
    ci_resumed: bool = False
    # Per phase: wall-clock seconds, and the tool calls the classifier denied.
    durations: dict[str, float] = field(default_factory=dict)
    permission_denials: dict[str, int] = field(default_factory=dict)


def outcome(report: dict, has_pr: bool) -> str:
    """The `Record.outcome` for a batch whose last phase printed `report`."""
    if report.get("is_error"):
        return "failed"
    blocker = BLOCKER.search(str(report.get("result") or ""))
    if blocker:
        return (
            "needs-input" if blocker.group(0).lower().startswith("needs") else "failed"
        )
    return "pr" if has_pr else NO_PR


def actionable(findings: Sequence[dict]) -> list[dict]:
    """The findings the fix round acts on, all at or above the confidence floor.

    A finding of medium severity or worse, or a vacuous `test` finding at any severity.
    """
    return [
        f
        for f in findings
        if float(f.get("confidence") or 0) >= CONFIDENCE_FLOOR
        and (
            f.get("severity") in ACTIONABLE_SEVERITIES
            or (
                f.get("category") == "test"
                and f.get("subkind") in ACTED_ON_TEST_SUBKINDS
            )
        )
    ]


def blocking(findings: Sequence[dict]) -> list[dict]:
    """The leftovers that hold a PR instead of landing it: medium or worse, confidence 0.8+.

    Above the fix round's 0.5 floor on purpose, so only a confident leftover stops a landing
    (#3951). Across 68 batches, 10 medium-or-worse findings survived the fix round and all 9
    of their PRs merged, three of them later named broken by a follow-up.
    """
    return [
        f
        for f in findings
        if f.get("severity") in ACTIONABLE_SEVERITIES
        and float(f.get("confidence") or 0) >= HOLD_CONFIDENCE
    ]


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


def held_for_findings(record: Record, state_dir: _Path) -> dict | None:
    """A `needs input:` report holding the open PR on a confident leftover, else None.

    On every host: a daniel-server batch's PR is landed by the orchestrator from `status`'s
    `done` line, which would land it just the same. A security finding is counted rather
    than named, because this text reaches `status` and the orchestrator's transcript.
    """
    left = blocking(record.remaining)
    if not left:
        return None
    named = [f"{f.get('title')} ({f.get('file')})" for f in left if not is_held(f)]
    held = len(left) - len(named)
    if held:
        named.append(f"{held} security findings in {state_dir}")
    return {
        "type": "result",
        "is_error": False,
        "result": (
            "needs input: the review left a confident medium-or-worse finding after the "
            f"fix round, so the PR was not landed: {'; '.join(named)}.\n{record.pr}"
        ),
    }


def held_for_green(record: Record, reason: str) -> dict:
    """The hold on a PR that fails the green gate, saying whether the reviewers faulted the fix.

    Without that, a correct fix held by a wrong red test reads like a wrong fix (#5400).
    """
    verdict, _, nodes = record.green_contest.partition(": ")
    rejected = nodes.split(", ") if verdict == "rejected" else []
    # `remaining` is the delta review's verdict, and stays empty when no fix round ran.
    left = record.remaining if "fix" in record.durations else record.actionable
    why = judgment(reason, left, record.review_error, rejected)
    return {
        "type": "result",
        "is_error": False,
        "result": (
            f"needs input: the PR fails the green gate, so it was not landed: {reason}. "
            f"{why}\n{record.pr}"
        ),
    }


def issues_section(brief: str) -> str:
    """The brief's fenced issue text, which is all the reviewer learns about intent."""
    _, sep, rest = brief.partition(ISSUES_HEADING)
    return sep + rest if sep else ""


def comment_body(record: Record) -> str:
    """The PR comment: the public trail of the review, its counts and its costs."""
    lines = ["## Fan-out review", ""]
    if record.review_error:
        lines += [f"The review did not complete: {record.review_error}.", ""]
    if record.red_gate == "passed":
        lines.append(
            f"Red gate passed: {record.red_tests} tests for {record.red_behaviours} stated "
            "behaviours failed on the unchanged code"
            + (
                f", {record.red_by_absence} of them only because a name was missing."
                if record.red_by_absence
                else "."
            )
        )
    elif record.red_gate:
        lines.append(f"Red gate refused the test author's commit: {record.red_gate}.")
    if record.green_gate:
        lines.append(f"Green gate: {record.green_gate}.")
    if record.green_contest.startswith("upheld: "):
        lines.append(
            "Green gate contested: a fresh reviewer upheld the implementer's cited contest "
            f"of {record.green_contest.removeprefix('upheld: ')}, so the PR went on with "
            "those red tests failing."
        )
    if record.ci_gate:
        lines.append(f"CI gate: {record.ci_gate}.")
    if record.red_hunks:
        noticed = record.red_hunks - len(record.red_hunks_missed)
        lines.append(
            f"The red tests noticed {noticed} of {record.red_hunks} fix hunks reverted one at "
            f"a time, {record.red_hunks_by_absence} of them only through a missing name."
        )
    if record.base_tests:
        lines.append(
            f"{len(record.base_passing)} of the PR's {record.base_tests} new tests pass with "
            "its code changes taken out."
        )
    if record.red_gate or record.base_tests or record.ci_gate:
        lines.append("")
    held = sum(1 for f in record.findings if is_held(f))
    lines.append(
        f"{len(record.findings)} findings, {len(record.actionable)} actionable "
        f"(severity medium or worse, or a vacuous test, at confidence "
        f"{CONFIDENCE_FLOOR} or more), "
        f"{len(record.remaining)} left after the fix round."
    )
    if held:
        lines.append(
            f"{held} security findings are held off this public page and kept in the "
            "orchestrator's local record."
        )
    # Only the actionable findings are listed (#3952): 77% of all findings sat below the
    # confidence floor, and the full list stays in the local record.
    shown = [f for f in record.actionable if not is_held(f)]
    below = len(record.findings) - len(record.actionable)
    if below:
        lines.append(
            f"{below} findings below the actionable bar are listed only in the local record."
        )
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
