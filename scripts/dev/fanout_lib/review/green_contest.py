"""The implementer's contest of a red test whose premise is false (#5400, #4874).

WHY. The red author writes the tests from the issue text alone, and an issue can be wrong
about the tree. In #4874 the red author assumed `claude-rc.service` ran on every host, but it
runs only on daniel-box, so the correct fix failed the red test. The implementer had no way
to say so that the pipeline read, the green gate held the PR, and it was landed by hand.

THE CONTEST. A fix round's reply contests the green gate when it says `<node> is wrong` of
every red node the gate's last run reports not passing and cites at least one `path:line`
(`contested`). Naming a node is not enough, since a fix reply names the test it made pass. The
delta reviewer is a fresh session that never saw the implementer's transcript, and
`review_prompts.delta_prompt` tells it to report the green gate's finding again when the cited
fact does not hold. A contest is upheld when the green gate's finding was among those the fix
round answered, the gate's last run ended `unmet`, and the delta review left no finding on
`GREEN_FILE` (`upheld`). A delta review that failed leaves every finding it was asked about,
so it upholds nothing. Only an `unmet` run can be contested: a refusal about what the fix
touched, an untracked config file or a failed clone says nothing about whether a test is
wrong.

The alternative #4875 named, having the red author verify each premise against the tree, was
not taken: the red author already reads the tree, and #4874's premise was one it did not think
to check. A second reader who sees the cited line catches that; a stricter brief does not.

AFTER. An upheld contest goes on past the failing red test, and the PR comment says so. CI runs
the whole suite on the PR, so the contested test fails there, and `review_ci.ci_hold` holds the
PR for a hand to correct the test rather than resuming the implementer. A resume that rewrote a
red test would face no green gate and no reviewer, and CI passing a test its author just
rewrote proves nothing.

A HOLD says what the reviewers made of the fix (`judgment`). It counts the findings the delta
review left, or the first review's when no fix round ran, and it points at the red test's
premise only when the gate failed on the red tests themselves (`unmet`).
"""

import re
from typing import TYPE_CHECKING

# Reach the sibling package: a directly-invoked script gets only its own directory on
# sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from fanout_lib.review.red_gate import GREEN_FILE, Gate, green_cause

if TYPE_CHECKING:
    from fanout_lib.review.review_record import Record

# A `path:line` citation: a file name with an extension that starts with a letter, one colon,
# then the line number. The single colon keeps a node id's `::` out, the extension a URL's
# scheme, and its letter a version such as `python3.12:1`.
CITATION = re.compile(r"[\w./-]+\.[A-Za-z]\w*:\d+\b")
# What a contest says of each node it disputes, right after naming it: `<node> is wrong`.
DISPUTE = r"`?\s+is\s+wrong\b"


def contested(reason: str, reply: str, gate: Gate | None) -> list[str]:
    """The red nodes `reply` contests in the green gate's `reason`, or [] for no contest.

    Args:
        reason: the green gate's refusal.
        reply: the fix round's final message.
        gate: the red gate's verdict, None for a batch without a red phase.
    """
    if gate is None or green_cause(reason) != "unmet" or not CITATION.search(reply):
        return []
    failing = [n for n in gate.nodes if f"{n} (" in reason]
    disputed = [n for n in failing if _disputes(reply, n)]
    return failing if failing and disputed == failing else []


def _disputes(reply: str, node: str) -> bool:
    """Whether `reply` says `<node> is wrong`, by its id or its test name as a whole word.

    A fix reply names the test it made pass and cites the line it changed as a matter of
    course, so naming the node alone is no contest.
    """
    name = re.escape(node.rsplit("::", 1)[-1])
    spelled = rf"(?:{re.escape(node)}|(?<![\w:]){name}(?![\w\[]))"
    return re.search(spelled + DISPUTE, reply) is not None


def upheld(
    reason: str, reply: str, gate: Gate, asked: list[dict], left: list[dict]
) -> list[str]:
    """The red nodes a contest the delta review upheld covers, or [] when none was upheld.

    Args:
        reason: the green gate's refusal on the fix round's head.
        reply: the fix round's final message.
        gate: the red gate's verdict.
        asked: the findings the fix round answered.
        left: the findings the delta review left unresolved.
    """
    answered = any(f.get("file") == GREEN_FILE for f in asked)
    if not answered or any(f.get("file") == GREEN_FILE for f in left):
        return []
    return contested(reason, reply, gate)


def settle(record: "Record", reason: str, reply: str, gate: Gate) -> str:
    """Record the fix round's contest of `reason`; the reason still holding, "" if upheld."""
    nodes = upheld(reason, reply, gate, record.actionable, record.remaining)
    if nodes:
        record.green_contest = f"upheld: {', '.join(nodes)}"
        return ""
    nodes = contested(reason, reply, gate)
    record.green_contest = f"rejected: {', '.join(nodes)}" if nodes else ""
    return reason


def judgment(
    reason: str, findings: list[dict], review_error: str, rejected: list[str]
) -> str:
    """What a green-gate hold on `reason` says about the reviewers' view of the fix.

    A hold with no other finding left is where a red test may be wrong, so the operator reads
    the two kinds of hold differently.

    Args:
        reason: the green gate's refusal.
        findings: the findings the reviewers left: the delta review's after a fix round, the
            first review's actionable ones when none ran.
        review_error: why a review did not complete, "" when every one did.
        rejected: the red nodes a fresh reviewer refused the implementer's contest of.
    """
    if review_error:
        return "The review did not complete, so no reviewer judged the fix."
    if rejected:
        return (
            "A fresh reviewer rejected the implementer's contest of "
            f"{', '.join(rejected)}."
        )
    others = [f for f in findings if f.get("file") != GREEN_FILE]
    if others:
        return f"The reviewers also left {len(others)} finding(s) against the fix."
    if green_cause(reason) != "unmet":
        return "The reviewers left no other finding against the fix."
    return (
        "The reviewers judged the fix correct and left no finding against it, so check the "
        "red test's premise."
    )
