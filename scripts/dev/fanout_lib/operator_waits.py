"""The textfile gauge of fan-out work that waits on a person, written by each lander pass (#5671).

WHY. Most of the fan-out pipeline's idle hours are work that waits on a person, and nothing
counted them as they happened. Over 2026-10-13 to 10-18, 15 PRs idled more than 2h before
their first land attempt, 207h in all. Every one was opened between 21:00 and 05:00 UTC, and
the lander held four of them about 10h each for touching its own surface (#5659).

WHERE IT RUNS. `fanout.py land-server` already reads daniel-server's review records over one
ssh call and the open PRs in one `gh` call, and `autoland.problems` already says why each PR
is held. A timer of its own would repeat both reads against the `ufw limit ssh` budget. So
the pass writes the gauge after it has judged every PR, from what it already read, plus two
local reads: daniel-box's own review records and the deployer's `owed.jsonl`.

WHAT COUNTS. Only a reason that a person must clear:

- `lander-surface`: the lander's own paths, or a file list at the listing cap.
- `operator-step`: an unchecked operator box, or "needs input" or "do not merge" text.
- `findings-remain`: findings left after the fix round, which hold the batch.
- `needs-input`: the batch ended `needs-input`.
- `manual-plane`: a setup role in `owed.jsonl` that the deployer cannot apply.

A transient reason, such as a head that moved since the review or a draft, waits on the
pipeline and is not counted. A PR-based wait ends when its PR closes or merges, so a record
counts only while its PR is open. Its age runs from the record's save time, which is when
the batch handed the PR over. On daniel-box, a batch lands its own PR whatever it touches,
so a daniel-box record counts only as `needs-input` or `findings-remain`.

DECIDED: `manual` issues are not counted. They are parked for the operator by design, so
their oldest age would always be the largest and would hide every other reason.

A pass that could not read daniel-server or GitHub writes nothing. The file then ages, and
node-exporter's `node_textfile_mtime_seconds` shows that, where a write of zeros would
report the server's waits as cleared.
"""

import json
import os
import re
import subprocess
import tempfile
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path

TEXTFILE = Path("/var/lib/node-exporter-textfile/fanout_operator_waits.prom")
OWED = Path("/var/lib/gitops-deploy/owed.jsonl")
REASONS = (
    "lander-surface",
    "operator-step",
    "findings-remain",
    "needs-input",
    "manual-plane",
)
# Each pattern matches the text `autoland.problems` returns for that reason. The test that
# pins this table feeds `problems` a record for every reason, so a rewording fails there.
_PROBLEM_REASONS = (
    (
        re.compile(r"^it changes the lander's own surface|at the listing cap$"),
        "lander-surface",
    ),
    (re.compile(r"^an unchecked box under |^the body says "), "operator-step"),
    (re.compile(r"^\d+ findings remain after the fix round$"), "findings-remain"),
    (re.compile(r"^the batch ended 'needs-input'"), "needs-input"),
)
_BOX_REASONS = frozenset({"findings-remain", "needs-input"})
_STAMP = re.compile(r"-(\d{8}T\d{6}Z)\.json$")


def reason_of(problem: str) -> str:
    """The waiting reason a `problems` text names, or "" when nobody has to act on it."""
    for pattern, reason in _PROBLEM_REASONS:
        if pattern.search(problem):
            return reason
    return ""


def saved_at(rec: Mapping) -> float | None:
    """The epoch second a review record was saved, from the stamp in its file name."""
    found = _STAMP.search(str(rec.get("file") or ""))
    if not found:
        return None

    return (
        datetime.strptime(found.group(1), "%Y%m%dT%H%M%SZ")
        .replace(tzinfo=UTC)
        .timestamp()
    )


def owed_manual_planes(text: str) -> list[float]:
    """The `at` second of each `manual_plane` line of `owed.jsonl`; a torn line is skipped."""
    out = []
    for line in text.splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        at = obj.get("at") if isinstance(obj, dict) else None
        if obj.get("class") == "manual_plane" and isinstance(at, (int, float)):
            out.append(float(at))
    return out


def waits(
    judged: Iterable[tuple[str, Mapping]],
    box: Iterable[tuple[str, Mapping]],
    manual_planes: Iterable[float],
) -> dict[str, list[float]]:
    """Each reason's wait start times.

    Args:
        judged: (problem text, record) for each daniel-server PR still open.
        box: (problem text, record) for each daniel-box PR still open.
        manual_planes: the start second of each owed manual-plane apply.
    """
    out: dict[str, list[float]] = {r: [] for r in REASONS}
    for host, pairs in (("server", judged), ("box", box)):
        for problem, rec in pairs:
            reason = reason_of(problem)
            if not reason or (host == "box" and reason not in _BOX_REASONS):
                continue
            start = saved_at(rec)
            if start is not None:
                out[reason].append(start)
    out["manual-plane"].extend(manual_planes)
    return out


def render(starts: Mapping[str, list[float]], now: float) -> str:
    """The textfile body: a count and an oldest age for every reason, 0 included."""
    lines = [
        "# HELP homelab_awaiting_operator Fan-out work that waits on a person, by reason.",
        "# TYPE homelab_awaiting_operator gauge",
    ]
    lines += [
        f'homelab_awaiting_operator{{reason="{r}"}} {len(starts.get(r, []))}'
        for r in REASONS
    ]
    lines += [
        "# HELP homelab_awaiting_operator_oldest_seconds Age of the oldest such wait; 0 when none.",
        "# TYPE homelab_awaiting_operator_oldest_seconds gauge",
    ]
    for r in REASONS:
        oldest = min(starts.get(r) or [now])
        lines.append(
            f'homelab_awaiting_operator_oldest_seconds{{reason="{r}"}} {max(0, int(now - oldest))}'
        )
    return "\n".join(lines) + "\n"


ISSUE_STATES = ("free", "claimed", "deferred", "manual")


def issue_states(issues: Iterable[Mapping], today: str) -> dict[str, int]:
    """How many open `claude` issues sit in each state, read from their labels alone (#5672).

    `manual` wins, then a `not-before:<date>` label later than `today` (`deferred`), then
    `claimed`; the rest are `free`. This is an approximation of `findings.py next`, which
    needs ssh to each batch's host to judge a claim and a PR read to withhold an issue an
    open PR closes. A stale claim counts as `claimed` here, so the count can read low, and an
    issue an open PR closes counts as `free`, so it can read high. On 2026-10-18 it read 54
    against `next`'s 56; the idle rule asks only whether the count is above 0.
    """
    out = dict.fromkeys(ISSUE_STATES, 0)
    for issue in issues:
        names = {
            str(lbl.get("name"))
            for lbl in issue.get("labels") or []
            if isinstance(lbl, dict)
        }
        later = [n for n in names if n.startswith("not-before:") and n[11:] > today]
        if "manual" in names:
            out["manual"] += 1
        elif later:
            out["deferred"] += 1
        elif "claimed" in names:
            out["claimed"] += 1
        else:
            out["free"] += 1
    return out


def render_queue(states: Mapping[str, int], capacity: Mapping[str, int]) -> str:
    """The issue-queue and batch-capacity families that `FanoutSlotsIdleWithFreeWork` reads.

    The live batch count is not written here: `claude_fanout_batch_pids_current`, from
    `claude-cgroup-metrics.sh`, already has one series per running batch on each host.
    """
    lines = [
        "# HELP findings_issues Open claude-labelled issues by state, from their labels.",
        "# TYPE findings_issues gauge",
        *(f'findings_issues{{state="{s}"}} {states.get(s, 0)}' for s in ISSUE_STATES),
        "# HELP fanout_batch_capacity The live fan-out batches a host admits.",
        "# TYPE fanout_batch_capacity gauge",
        *(
            f'fanout_batch_capacity{{host="{h}"}} {n}'
            for h, n in sorted(capacity.items())
        ),
    ]
    return "\n".join(lines) + "\n"


def write(path: Path, body: str) -> bool:
    """Write `body` to `path` atomically, readable by node-exporter; False when the directory is gone.

    mktemp in the directory, chmod 0644, then rename, because a bare temporary file is 0600
    and the exporter's user could not read it (M3's rule).
    """
    if not path.parent.is_dir():
        return False
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(body)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except OSError:
        Path(tmp).unlink(missing_ok=True)
        raise
    return True


def read_owed(path: Path = OWED) -> str:
    """The deployer's owed ledger, or "" when it is absent or unreadable."""
    try:
        return path.read_text()
    except OSError:
        return ""


def read_local_records(reader: str) -> list[dict]:
    """daniel-box's own recent review records, read by the same script daniel-server runs."""
    done = subprocess.run(
        ["python3", "-I", "-"],
        input=reader,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    try:
        data = json.loads(done.stdout) if done.returncode == 0 else []
    except ValueError:
        return []
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []
