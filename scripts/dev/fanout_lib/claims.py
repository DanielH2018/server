"""The claims a fan-out takes under the orchestrator's own branch, and the batch specs they cover.

`fanout_place.py launch` claims this repo's issues itself, batch by batch, immediately before
each batch's agent starts; `fanout_place.py claim` takes the same claims for an orchestrator
that spawns its own subagents (#3695). Another repo's batches are claimed under their own
branch instead, by `fanout_lib.launch`.
"""

import re
import subprocess
import sys

# Reach the sibling package: a directly-invoked script gets only its own directory on
# sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from fanout_lib.brief import WORKED_BY
from fanout_lib.target import Target
from fanout_lib.transport import Tools, error_text

BATCH_RE = re.compile(r"^\d+(,\d+)*$")


def parse_batches(specs: list[str]) -> dict[str, list[int]] | None:
    """Map each `--batch` spec to its issue numbers, or None when a spec is unusable.

    An identical spec given twice is placed once and reported; the same issue number in two
    different specs is refused — launching it twice means two agents in two worktrees on one
    issue, which the claim in the brief cannot undo.
    """
    batches: dict[str, list[int]] = {}
    seen: dict[int, str] = {}
    for spec in specs:
        if not BATCH_RE.match(spec):
            print(
                f"launch: --batch takes issue numbers joined by commas, got {spec!r}",
                file=sys.stderr,
            )
            return None
        key = spec.replace(",", "-")
        if key in batches:
            print(
                f"launch: --batch {spec} given twice; placing it once", file=sys.stderr
            )
            continue
        numbers = [int(n) for n in spec.split(",")]
        for n in numbers:
            if n in seen and seen[n] == spec:
                print(
                    f"launch: issue {n} is listed twice in --batch {spec}",
                    file=sys.stderr,
                )
                return None
            if n in seen:
                print(
                    f"launch: issue {n} appears in more than one --batch "
                    f"({seen[n]}, {spec}); refusing to launch it twice",
                    file=sys.stderr,
                )
                return None
            seen[n] = spec
        batches[key] = numbers
    return batches


def orchestrator_branch(tools: Tools, target: Target, verb: str) -> str | None:
    """The branch this repo's claims go under, or None having said why it refused.

    The branch is HEAD of the cwd, the orchestrator's own worktree. A detached HEAD reads
    `HEAD`, and the base branch is the primary checkout's: a claim under either names no
    worktree that ends when the fan-out does, so `findings.py` would judge it against the
    wrong tree for as long as the claim stands.

    Another repo's batch is claimed under its own branch (`launch._claim`), so for it this
    name only records in the manifest who ran the fan-out, and nothing is refused.
    """
    try:
        branch = tools.head_branch()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        if not target.is_server:
            return "HEAD"
        print(f"{verb}: could not read HEAD: {error_text(exc)}", file=sys.stderr)
        return None
    if target.is_server and branch in ("HEAD", target.base_branch):
        print(
            f"{verb}: HEAD is {branch!r}; run this from the orchestrator's own worktree, "
            "whose branch holds the claims",
            file=sys.stderr,
        )
        return None
    return branch


def reap_register(tools: Tools, target: Target) -> None:
    """Run `findings.py reap` on the batches' register before `launch` claims anything.

    The issue-fanout skill had the orchestrator run `reap` by hand, as the one place anything
    invokes it (#3943). Here it runs on every launch instead. A failed reap only warns: each
    `claim` still reaps a stale claim on the issue it takes, and `reap` releases nothing when
    its git read fails.
    """
    argv = ["reap"] if target.is_server else ["reap", "--repo", target.repo]
    try:
        proc = tools.findings(argv)
    except subprocess.TimeoutExpired:
        print("launch: reap timed out; claiming without it", file=sys.stderr)
        return
    for line in proc.stdout.splitlines():
        print(f"launch: {line}")
    if proc.returncode != 0:
        detail = " ".join(proc.stderr.split())
        print(
            f"launch: reap failed ({proc.returncode}): {detail}; claiming without it",
            file=sys.stderr,
        )


def post_worked_by(
    tools: Tools, issues: list[int], branch: str, target: Target
) -> None:
    """Record on each issue which batch branch took it, once that batch is running.

    The agent posted this as its first act, a model turn per issue (237 calls in the
    transcripts, #3962), and a branch name in backticks inside double quotes made the shell
    run it as a command. `launch` knows the branch, so it posts the comment itself. A failed
    post only warns: the claim, not this comment, is what keeps a second session off the issue.
    """
    for number in issues:
        if not tools.comment(number, f"{WORKED_BY}{branch}`", target.repo):
            print(
                f"launch: #{number}: could not post the Worked-by comment",
                file=sys.stderr,
            )


def claim_for_orchestrator(
    tools: Tools, issues: list[int], holder: str
) -> tuple[list[int], list[str]]:
    """Claim each of this repo's `issues` under the orchestrator's branch `holder`.

    One `findings.py claim` per issue, because its exit status is the only per-issue verdict
    it gives: one call for a whole batch exits 3 when any issue is refused, and which one was
    is then only in its prose. `claim` already does the rest of the work — it refuses an
    issue another worktree live-holds, reaps a stale claim on the way past, and refuses a
    holder that would read stale at birth.

    Returns:
        (claimed, refusals): the issues now held under `holder`, in input order, and one
        line per issue that was not, naming why.
    """
    claimed, refusals = [], []
    for number in issues:
        try:
            proc = tools.findings(["claim", str(number), "--worktree", holder])
        except subprocess.TimeoutExpired:
            refusals.append(f"#{number}: claim timed out")
            continue
        if proc.returncode == 0:
            claimed.append(number)
            continue
        detail = " ".join((proc.stdout + proc.stderr).split())
        refusals.append(f"#{number}: claim refused ({proc.returncode}): {detail}")
    return claimed, refusals


def release_for_orchestrator(tools: Tools, issues: list[int], holder: str) -> str:
    """Release `issues` from `holder` after a refused launch; a message suffix on failure."""
    argv = ["release", *(str(n) for n in issues), "--worktree", holder]
    try:
        released = tools.findings([*argv, "--reason", "fan-out launch refused"])
    except subprocess.TimeoutExpired:
        return "; release timed out"
    if released.returncode != 0:
        return f"; release failed ({released.returncode})"
    return f"; released {', '.join(f'#{n}' for n in issues)}"


def claim_all(tools: Tools, batches: dict[str, list[int]], target: Target) -> int:
    """Claim every batch's issues under HEAD, launching nothing; `fanout_place.py claim`.

    Returns:
        The exit code: 0 when every issue was claimed, 3 when any was refused, 1 when HEAD
        names no branch a claim can live under.
    """
    holder = orchestrator_branch(tools, target, "claim")
    if holder is None:
        return 1
    numbers = [n for issues in batches.values() for n in issues]
    claimed, refusals = claim_for_orchestrator(tools, numbers, holder)
    for number in claimed:
        print(f"#{number} claimed by `{holder}`")
    for reason in refusals:
        print(f"claim: {reason}", file=sys.stderr)
    return 3 if refusals else 0


def claim_batch(
    tools: Tools, batch: str, issues: list[int], holder: str
) -> tuple[str, list[int], bool]:
    """Claim one batch's issues, dropping the refused ones and renaming the batch to match.

    Returns:
        (batch, claimed, refused): the batch id rebuilt from the issues it kept, those
        issues, and whether any was dropped. An empty `claimed` means launch nothing.
    """
    claimed, refusals = claim_for_orchestrator(tools, issues, holder)
    for reason in refusals:
        print(f"{batch}: dropped {reason}", file=sys.stderr)
    if not claimed:
        print(f"{batch}: no issue claimed; not launching it", file=sys.stderr)
    return "-".join(str(n) for n in claimed), claimed, bool(refusals)
