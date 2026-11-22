"""What one `--review` batch records: its phases, the findings, and the PR comment built from them.

Split from `review.py`, which runs the phases, so the record's shape and its rendering can grow
without that module crossing its length cap. `review.py` re-exports every name here.
"""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

# Reach the sibling package: a directly-invoked script gets only its own directory on
# sys.path, and pyproject's `pythonpath` is a pytest setting.
from pathlib import Path as _Path


from dev.fanout_lib.brief import ISSUES_HEADING
from dev.fanout_lib.sizing import DEFAULT, REVIEWER, Rung
from dev.fanout_lib.status import BLOCKER, NO_PR

ACTIONABLE_SEVERITIES = frozenset({"critical", "high", "medium"})
# DECIDED: 0.5, not 0.6 (#3675). Judged against 86 batches' records, 5 of the 6 medium findings
# at 0.5-0.55 were material defects the fix round never saw; at 0.45 it was 3 of 7.
CONFIDENCE_FLOOR = 0.5
HOLD_CONFIDENCE = 0.8
# A record's `blocker` keeps this much of the line, enough to read why the batch stopped.
BLOCKER_CHARS = 500
# The name `review.Pipeline._save` gives a record, `<batch>-<saved>.json`, as its readers parse it.
SAVED_FORMAT = "%Y%m%dT%H%M%SZ"
RECORD_NAME = re.compile(r"^(?P<batch>\d+)\S*-(?P<saved>\d{8}T\d{6}Z)\.json$")
# The `test` findings the fix round takes at any severity: a test that checks nothing. The
# severity floor would drop most of the reviewer's test findings, these among them. A
# `scaffold` finding is mostly a duplicate of a stronger test, while a vacuous one is real, so
# a scaffold or missing-coverage finding keeps the severity floor.
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
    # and actionable findings per PR can be compared between settings. A record
    # written before this field existed has no key.
    review_effort: str = REVIEWER.effort
    # The `fanout_lib.sizing` rung every phase that runs or resumes the implementer passes,
    # read from here for the same reason as `review_effort`.
    implementer: Rung = DEFAULT
    costs: dict[str, float] = field(default_factory=dict)
    review_error: str = ""
    findings: list[dict] = field(default_factory=list)
    actionable: list[dict] = field(default_factory=list)
    remaining: list[dict] = field(default_factory=list)
    # The findings the delta review reported without a quote the fix head's file holds near
    # the cited line (`delta_confirm`). They are carried, not left: they hold nothing.
    unverified: list[dict] = field(default_factory=list)
    # How many fix rounds ran: 0 with no actionable finding, 2 when a medium-or-worse finding
    # survived the first delta review.
    fix_rounds: int = 0
    # The PR's new test nodes, those of them that pass with its code changes taken out
    # (`base_check`), and why the check could not run, if it could not.
    base_tests: int = 0
    base_passing: list[str] = field(default_factory=list)
    base_error: str = ""
    # How the batch ended, so the records count the batches that never reached a PR:
    # `failed`, `needs-input` or `no-pr` before a PR exists, then `pr` once one does, or
    # `failed` / `needs-input` when the last phase says so.
    outcome: str = ""
    # The `needs input:` or `failed:` line the last phase's report ended on, "" when it ended
    # on none, so the records can say why a batch stopped as well as that it did.
    blocker: str = ""
    # The head the last reviewer read: the head the first review read, or the fix round's head
    # when a delta review read that. `fanout_lib.autoland` lands a PR unattended only
    # while GitHub still shows this head, so a push after the review stops at the PR. "" in a
    # record written before this field existed.
    head: str = ""
    # The VERDICT line of the landing the pipeline ran, "" when it ran none.
    verdict: str = ""
    # Why a daniel-server pipeline left its PR for the server lander rather than landing it
    # (`review_ci.server_lands`): "" when it landed it, or on daniel-box.
    land_hold: str = ""
    # True when the server lander's PR rule (`own_surface.pr_problems`) made that hold. The PR
    # then stays a draft with no CI until a person's `land.sh` marks it ready at its turn.
    person_lands: bool = False
    # The CI wait a batch that does not land runs: "" when it ran none, else its
    # result in words, whether the implementer was resumed for a red verdict, and whether
    # the failed jobs were re-run after a resume that pushed nothing.
    ci_gate: str = ""
    ci_resumed: bool = False
    ci_rerun: bool = False
    # The jobs the first CI attempt failed, kept when the gate re-ran them: `ci_gate`
    # names jobs only while the head is still red. [] in a record written before this field.
    ci_rerun_jobs: list[str] = field(default_factory=list)
    # Per phase: wall-clock seconds, and the tool calls the classifier denied.
    durations: dict[str, float] = field(default_factory=dict)
    permission_denials: dict[str, int] = field(default_factory=dict)
    # True for the record of a unit `fanout.py place resume` started, which ran only the
    # conflict phases. The batch's first record carries the rest, so `review_stats` counts
    # this one's cost and no second batch.
    resumed: bool = False

    def settle(self, report: dict, has_pr: bool) -> None:
        """Set `outcome` and `blocker` from the report the batch's last phase printed."""
        self.outcome = outcome(report, has_pr)
        self.blocker = blocker(report)


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


def blocker(report: dict) -> str:
    """The `needs input:` or `failed:` line in `report`'s result, capped at `BLOCKER_CHARS`."""
    text = str(report.get("result") or "")
    found = BLOCKER.search(text)
    if not found:
        return ""
    return text[found.start() :].split("\n", 1)[0].strip()[:BLOCKER_CHARS]


def is_held(finding: dict) -> bool:
    """Whether a finding stays off the public PR and tracker for disclosure reasons."""
    return finding.get("category") == "security"


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


def survivors(remaining: Sequence[dict]) -> list[dict]:
    """The findings left after a delta review that earn a second fix round.

    Medium severity or worse, which excludes a vacuous `test` finding that `actionable` takes
    at any severity. 12 of the 13 batches that ended `needs-input` over 2026-10-25 to
    2026-11-08 with findings left held a real defect, so the confidence floor stays.
    """
    return [f for f in remaining if f.get("severity") in ACTIONABLE_SEVERITIES]


def blocking(findings: Sequence[dict]) -> list[dict]:
    """The leftovers that hold a PR instead of landing it: medium or worse, confidence 0.8+.

    Above the fix round's 0.5 floor on purpose, so only a confident leftover stops a landing.
    Across 68 batches, 10 medium-or-worse findings survived the fix round and all 9
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


def held_for_findings(
    record: Record, state_dir: _Path, after: str = "the fix round"
) -> dict | None:
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
            "needs input: the review left a confident medium-or-worse finding after "
            f"{after}, so the PR was not landed: {'; '.join(named)}.\n{record.pr}"
        ),
    }


def reviewed_head(state_dir: _Path, batch: str) -> str:
    """The head the newest record of `batch` under `state_dir` says a reviewer read, or ""."""
    saved = re.compile(rf"{re.escape(batch)}-\d{{8}}T\d{{6}}Z\.json")
    if not state_dir.is_dir():
        return ""
    for path in sorted(state_dir.iterdir(), reverse=True):
        if not saved.fullmatch(path.name):
            continue
        try:
            record = json.loads(path.read_text())
        except OSError, ValueError:
            continue
        if isinstance(record, dict) and record.get("head"):
            return str(record["head"])
    return ""


def issues_section(brief: str) -> str:
    """The brief's fenced issue text, which is all the reviewer learns about intent."""
    _, sep, rest = brief.partition(ISSUES_HEADING)
    return sep + rest if sep else ""


def comment_body(record: Record) -> str:
    """The PR comment: the public trail of the review, its counts and its costs."""
    lines = ["## Fan-out review", ""]
    if record.review_error:
        lines += [f"The review did not complete: {record.review_error}.", ""]
    if record.ci_gate:
        lines.append(f"CI gate: {record.ci_gate}.")
    if record.land_hold:
        lines.append(f"Not landed by the pipeline: {record.land_hold}.")
    if record.person_lands:
        lines.append(
            "It stays a draft: `land.sh` marks it ready and runs its CI at its turn."
        )
    if record.base_tests:
        lines.append(
            f"{len(record.base_passing)} of the PR's {record.base_tests} new tests pass with "
            "its code changes taken out."
        )
    if record.base_tests or record.ci_gate:
        lines.append("")
    held = sum(1 for f in record.findings if is_held(f))
    lines.append(
        f"{len(record.findings)} findings, {len(record.actionable)} actionable "
        f"(severity medium or worse, or a vacuous test, at confidence "
        f"{CONFIDENCE_FLOOR} or more), "
        f"{len(record.remaining)} left after the fix round."
    )
    if record.fix_rounds > 1:
        lines.append(
            "A second fix round ran because the first delta review left a finding of "
            "medium severity or worse."
        )
    if record.unverified:
        lines.append(
            f"{len(record.unverified)} more carried unverified: the delta review reported "
            "them without quoting a line the fix head still has, so they do not hold the PR."
        )
    if held:
        lines.append(
            f"{held} security findings are held off this public page and kept in the "
            "orchestrator's local record."
        )
    # Only the actionable findings are listed: 77% of all findings sat below the
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
