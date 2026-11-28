"""One row for each open change: its stage, its state within the stage, and since when.

A change is keyed by its PR, or by its fan-out batch before the batch has a PR. Each stage has
an owner, and the row's state is that owner's own word:

- implementation and review: the batch's running phase, which the fan-out pipeline writes to
  `.fanout/phase` and `fanout.py board` reads;
- waiting to land: the PR's draft flag and CI, from the PR list the page caches;
- landing and deploying: the landing's state record, split at the merge;
- ended: the landing's verdict, with the `next` action from its `LAND-RESULT:` line, or the
  batch's own state when it stopped without a PR.

This module is the one mapping from those sources to a stage. The daemon cannot import
land_lib or fanout_lib, so the landing states, the `next` actions and the review phase below
are copies; the deploy UI's CLAUDE.md, *Changes*, names the test that holds each.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from ci_verdict import CIVerdict

from deploy_ui_lib import deploy_ui_states as states


class Stage(StrEnum):
    IMPLEMENTATION = "implementation"
    REVIEW = "review"
    WAITING = "waiting to land"
    LANDING = "landing"
    DEPLOYING = "deploying"
    ENDED = "ended"


_STAGE_RANK = {s: n for n, s in enumerate(Stage)}

# The landing states before the merge, and those from the merge to the verdict.
LANDING_STATES = frozenset(
    {
        "armed",
        "handed-off",
        "own-run",
        "queued",
        "holding",
        "yielded",
        "testing",
        "split",
        "proving",
        "merging",
        "rejoining",
        "no-turn",
    }
)
DEPLOYING_STATES = frozenset(
    {"merged", "resolving", "master-ci", "promoting", "host-wait", "grading"}
)
# What `review/batch_run.py` writes to `.fanout/phase` while a reviewer session runs.
REVIEW_PHASE = "review"

DONE = "done"
# What a change waits on after a landing that named each `next` action.
NEXT_LABELS: dict[str, str] = {
    DONE: "nothing: the landing is done",
    "rerun": "a re-run of land.sh",
    "wait:landing-clear": "master CI or DEPLOY_HOLD to clear, then a re-run",
    "follow:land": "the PR's other landing",
    "fix-and-rerun": "a fix to the PR, then a re-run",
    "dispatch-and-rerun": "the workflow runs the verdict names, then a re-run",
    "fix-builder-and-rerun": "a fix to the host package builder, then a re-run",
    "apply-by-hand": "an apply by hand",
    "operator": "the operator",
}

# A batch that stopped this long ago with no open PR and no landing is not shown.
WINDOW_S = states.WINDOW_S
# A merged change whose landing needs nothing more, or whose next action this host cannot
# read, shows its verdict this long; a verdict that needs an action shows for `WINDOW_S`.
FINISHED_S = 3600


def landing_stage(state: str) -> Stage:
    """The stage of a landing in `state`; a state this module does not know is landing."""
    if state == states.VERDICT:
        return Stage.ENDED
    return Stage.DEPLOYING if state in DEPLOYING_STATES else Stage.LANDING


@dataclass(frozen=True)
class BatchRow:
    """One fan-out batch, as one row of `fanout.py board`.

    Attributes:
        running: whether its unit is active; None when its host's read timed out.
        phase: the pipeline phase `.fanout/phase` names, or "".
        phase_since: the epoch second that phase began, or None.
        ended_at: the epoch second its report was last written, or None.
        next: the `next` field of the newest `LAND-RESULT:` line in its worktree, or "".
    """

    batch: str
    host: str
    branch: str
    state: str
    running: bool | None
    phase: str
    phase_since: float | None
    launched_at: float | None
    ended_at: float | None
    pr: str
    next: str

    @classmethod
    def parse(cls, raw: dict) -> "BatchRow":
        """The row from one parsed board object.

        Raises:
            KeyError, TypeError, ValueError: the object is not a board row.
        """
        url = str(raw.get("pr_url") or "")
        return cls(
            batch=str(raw["batch"]),
            host=str(raw["host"]),
            branch=str(raw["branch"]),
            state=str(raw["state"]),
            running=None if raw["running"] is None else bool(raw["running"]),
            phase=str(raw.get("phase") or ""),
            phase_since=_epoch(raw.get("phase_since")),
            launched_at=_epoch(raw.get("launched_at")),
            ended_at=_epoch(raw.get("ended_at")),
            pr=url.rstrip("/").rsplit("/", 1)[-1] if "/pull/" in url else "",
            next=str(raw.get("next") or ""),
        )

    def last_seen(self) -> float | None:
        return self.ended_at or self.phase_since or self.launched_at


def _epoch(value) -> float | None:
    return None if value in (None, "") else float(value)


@dataclass(frozen=True)
class Place:
    """Where a change stands: its stage, the owner's word for its state, and what it waits on.

    Attributes:
        label: the state as the page shows it.
        since: the epoch second the change entered `state`, or None when no source says.
        next: the `next` action of the landing that ended it, or "".
    """

    stage: Stage
    state: str
    label: str
    since: float | None
    waits_on: str
    next: str = ""


@dataclass(frozen=True)
class BatchCell:
    """The batch column; `state` is "" while its host's read is unavailable."""

    id: str
    host: str
    state: str


@dataclass(frozen=True)
class ChangeRow:
    """One change, as `/api/changes` serializes it."""

    key: str
    pr: str
    title: str
    stage: str
    state: str
    label: str
    since: float | None
    in_state_s: int | None
    waits_on: str
    next: str
    batch: BatchCell | None


def _change(
    key: str, pr: str, title: str, place: Place, b: BatchRow | None, now: float
) -> ChangeRow:
    since = place.since
    return ChangeRow(
        key=key,
        pr=pr,
        title=title,
        stage=str(place.stage),
        state=place.state,
        label=place.label,
        since=since,
        in_state_s=None if since is None else max(0, int(now - since)),
        waits_on=place.waits_on,
        next=place.next,
        batch=None
        if b is None
        else BatchCell(b.batch, b.host, "" if b.running is None else b.state),
    )


def _landing_place(landing: dict) -> Place:
    """A live landing's place, from its `deploy_ui_states.landing_rows` row."""
    state, progress = landing["state"], landing["progress"]
    if landing["alive"] is False:
        waits = "nothing: its landing process is gone; re-run land.sh"
    elif landing["queue"] == "waiting":
        waits = f"the turn, place {landing['position']} in the queue"
    elif landing["queue"] == "stepped-out":
        waits = "the turn, which it stepped out of"
    elif progress:
        waits = f"{progress['done']}/{progress['total']} {progress['what']}"
    else:
        waits = f"its {states.label(state)} step"
    label = states.label(state)
    return Place(landing_stage(state), state, label, landing["since"], waits)


def _ended_place(record: states.Record, next_action: str) -> Place:
    waits = NEXT_LABELS.get(next_action, next_action) or (
        "its LAND-RESULT line, which this host cannot read"
    )
    verdict = record.verdict
    return Place(Stage.ENDED, verdict, verdict, record.since, waits, next_action)


def _batch_place(b: BatchRow) -> Place:
    if b.running:
        stage = Stage.REVIEW if b.phase == REVIEW_PHASE else Stage.IMPLEMENTATION
        # A `--no-review` batch runs one session and writes no phase.
        state = b.phase or b.state
        since = b.phase_since or b.launched_at
        return Place(stage, state, state, since, f"its {state} session")
    waits = "its orchestrator, which reads the batch's final message"
    return Place(Stage.ENDED, b.state, b.state, b.ended_at, waits)


def _waiting_place(pr: dict, batch: BatchRow | None) -> Place:
    """An open PR's place, from its `deploy_ui_reads.parse_prs` row."""
    ready = "draft" if pr["draft"] else "ready"
    if pr["ci"] == CIVerdict.FAIL:
        waits = "a fix for its red CI"
    elif pr["draft"]:
        waits = "a land.sh run; a draft's CI runs at its turn"
    elif pr["ci"] == CIVerdict.PENDING:
        waits = "its CI"
    else:
        waits = "a land.sh run"
    since = batch.ended_at if batch and batch.ended_at else pr.get("created")
    return Place(Stage.WAITING, ready, f"{ready}, CI {pr['ci']}", since, waits)


def change_rows(
    prs: Sequence[dict] | None,
    landings: Sequence[dict],
    ended: Sequence[states.Record],
    batches: Sequence[BatchRow] | None,
    branch_prs: Mapping[str, str],
    results: Mapping[str, str],
    titles: Mapping[str, str],
    now: float,
) -> list[ChangeRow]:
    """One row per open change, ordered by stage, then by how long it has been in its state.

    Args:
        prs: the cached open PR list (`deploy_ui_reads.parse_prs` rows), or None when it
            could not be read.
        landings: the live landings, as `deploy_ui_states.landing_rows` gives them.
        ended: each PR's newest record that names its verdict, within the records' window.
        batches: the fan-out batches, or None when no host's board could be read.
        branch_prs: each branch the daemon has seen an open PR for, to that PR's number, so
            a batch keeps its PR after the merge drops the PR from the list.
        results: each PR's `next` action from the newest `LAND-RESULT:` line this host
            can read.
        titles: PR titles already fetched, for a PR the open list no longer holds.
        now: the epoch second the rows are measured at.

    Returns:
        A change's newest source decides its stage: a live landing beats a running batch,
        which beats a verdict older than its phase.
    """
    open_prs = {str(p["number"]): p for p in prs or ()}
    live = {r["pr"]: r for r in landings}
    verdicts = {r.pr: r for r in ended}
    by_pr: dict[str, BatchRow] = {}
    lone: list[BatchRow] = []
    for b in batches or ():
        pr = branch_prs.get(b.branch) or b.pr
        if pr:
            by_pr[pr] = b
        else:
            lone.append(b)

    rows = []
    for pr in sorted({*open_prs, *live, *verdicts, *by_pr}, key=int):
        b, verdict = by_pr.get(pr), verdicts.get(pr)
        if pr in live:
            place = _landing_place(live[pr])
        elif b and b.running and not (verdict and verdict.since > (b.phase_since or 0)):
            place = _batch_place(b)
        elif verdict:
            next_action = b.next if b and b.next else results.get(pr, "")
            finished = next_action in (DONE, "") and pr not in open_prs
            if finished and now - verdict.since > FINISHED_S:
                continue
            place = _ended_place(verdict, next_action)
        elif pr in open_prs:
            place = _waiting_place(open_prs[pr], b)
        elif b and b.running is not None and _recent(b, now):
            place = _batch_place(b)
        else:
            continue
        title = (open_prs.get(pr) or {}).get("title") or titles.get(pr, "")
        rows.append(_change(f"pr:{pr}", pr, title, place, b, now))
    for b in lone:
        # A batch whose host did not answer has no stage to show until it does.
        if b.running is not None and _recent(b, now):
            key = f"batch:{b.host}:{b.batch}"
            rows.append(_change(key, "", "", _batch_place(b), b, now))
    rows.sort(key=lambda r: (_STAGE_RANK[Stage(r.stage)], r.since or now))
    return rows


def _recent(b: BatchRow, now: float) -> bool:
    seen = b.last_seen()
    return bool(b.running) or (seen is not None and now - seen <= WINDOW_S)
