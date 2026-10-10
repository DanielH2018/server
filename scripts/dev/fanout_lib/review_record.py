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

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from fanout_lib.brief import ISSUES_HEADING
from fanout_lib.review_prompts import is_held
from fanout_lib.status import BLOCKER, NO_PR

ACTIONABLE_SEVERITIES = frozenset({"critical", "high", "medium"})
CONFIDENCE_FLOOR = 0.6


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
    # How the batch ended, so the records count the batches that never reached a PR (#3940):
    # `failed`, `needs-input` or `no-pr` before a PR exists, then `pr` once one does, or
    # `failed` / `needs-input` when the last phase says so.
    outcome: str = ""
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
    """The findings the fix round acts on: medium or worse, at or above the confidence floor."""
    return [
        f
        for f in findings
        if f.get("severity") in ACTIONABLE_SEVERITIES
        and float(f.get("confidence") or 0) >= CONFIDENCE_FLOOR
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
