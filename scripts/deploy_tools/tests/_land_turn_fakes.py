"""The landing-turn harness the turn and rejoin tests share."""

from _land_fakes import Fakes
from deploy_tools.land_lib import merge, turn

HEAD = "a" * 40
OPEN = {"state": "OPEN", "mergeable": "MERGEABLE", "headRefOid": HEAD}
MERGED = {"state": "MERGED", "mergeable": "MERGEABLE", "headRefOid": HEAD}
# The head a branch update makes.
UPDATED = "b" * 40
# The fields `merge._start_run` reads before the first poll.
START_VIEW = "isDraft,state,mergeable,headRefOid"
# Master moved on a path the PR leaves alone, so only the turn's own rule updates the branch.
MASTER_MOVED = [["docs/other.md"]]
# A run that ran jobs on the head, which a landing never holds the turn to wait for.
A_RUN = [{"id": 2, "status": "in_progress"}]


def turn_landing(landing, polls=(OPEN, MERGED), start=OPEN, **fakes_or_opts):
    """A draft's landing; its head reads green on each of `polls` that is open.

    `start` is the PR as the landing reads it before its first poll, or a tuple of what it
    reads before it stands in line for the turn and after.
    """
    starts = start if isinstance(start, tuple) else (start,)
    fakes = {
        k: fakes_or_opts.pop(k)
        for k in (
            "turn",
            "serial",
            "line",
            "await_ci",
            "gh_merge_rc",
            "gh_stderr",
            "compare",
            "head_runs",
        )
        if k in fakes_or_opts
    }
    ln, calls = landing(
        Fakes(
            gh_views={
                "state,mergeable,headRefOid": list(polls),
                START_VIEW: [{"isDraft": True, **s} for s in starts],
            },
            **fakes,
        ),
        **fakes_or_opts,
    )
    ln.direct_merge_subject = "Take turns"
    return ln, calls


def land_in_turn(ln) -> None:
    """The merge wait as the pipeline runs it."""
    with turn.held(ln) as take_turn:
        merge.await_merge(ln, take_turn)


def first_gh(calls) -> int:
    return next(i for i, (name, _, _) in enumerate(calls) if name == "gh")


def call_order(calls) -> list[str]:
    """The calls that matter to the turn, in order, each under a short name."""
    names = []
    for name, args, _ in calls:
        if name == "gh" and "PUT" in args:
            names.append("update" if args[3].endswith("/update-branch") else "merge")
        elif name == "gh":
            names.append(" ".join(args[:2]))
        elif name in ("try_turn", "release_turn"):
            names.append(name)
    return names


def line_calls(calls) -> list[tuple[str, ...]]:
    return [args for name, args, _ in calls if name == "line"]


def merged_sha(calls) -> str:
    args = next(
        c[1] for c in calls if c[0] == "gh" and any(a.endswith("/merge") for a in c[1])
    )
    return next(a for a in args if a.startswith("sha=")).removeprefix("sha=")
