# ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_render_bases.py
"""The render bases a `manifests` line keeps, and why it is still kept (#5118, #5122).

THE BASE OUTLIVES THE RENDER RECORD. `deploy_k8s_owed._guard_row_carries` asks whether a
caller carries a map-only change from a commit B it is proven at. A matching render is one such
B, but the hourly producer overwrites each caller's single render record at the newest green
commit. A render at or after the line's origin that matches discharges the line by itself. One
that does not match left the guard arm only the release record, which may predate an earlier
role change the old render had proved (#5122). So each tick stores, on the line, every caller's
newest matching render as `[render commit, release commit]`. The pair stays a base only while
the caller's release record still names that release commit. Any later apply rewrites the
record, a rollback included, and the bytes the render vouched for may no longer be live.

The pairs ride on the `k8s_unapplied` ledger line as an extra key, which `rewrite_owed` carries
through every origin move. They go when the line goes, so a new line starts from the renders on
disk at its first tick. The functions here only transform the ledger's text:
`deploy_k8s_owed` stays its one reader and writer, as that module's docstring requires.

WHY A LINE IS STILL KEPT. A line waiting on renders the producer has not yet made logged nothing
that told it apart from a render that never matches. #5118's line was cleared by hand eight
minutes before the first render at or after its origin started. `log_waits` names each caller
that still holds the line and which of the two it is.

Stdlib and `gitops_ledger` only, for the reason `deploy_release.py` gives.
"""

import json

from deployer_lib.deploy_config import log
from gitops_ledger import OWED_K8S_UNAPPLIED, owed_line_key

BASES_KEY = "render_bases"
# The reasons `log_waits` gives for a caller that holds a `manifests` line. Only the first
# clears without a deploy: the producer has not yet rendered a commit at or after the change.
NOT_RENDERED = "its newest render predates the change"
NO_MATCH = "a render at or after the change does not match what it applied"
NO_RENDER = "it has no render record"


def _line_obj(line: str, subject: str) -> dict | None:
    """`line` decoded when it is `subject`'s `k8s_unapplied` line, else None."""
    if owed_line_key(line) != (OWED_K8S_UNAPPLIED, subject):
        return None
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def stored_bases(owed: str | None, subject: str) -> dict[str, list[str]]:
    """`{tag: [render commit, release commit]}` from `subject`'s line in `owed`, or {}.

    A pair that is not two strings is dropped rather than trusted.
    """
    for line in (owed or "").splitlines():
        obj = _line_obj(line, subject)
        if obj is None:
            continue
        bases = obj.get(BASES_KEY)
        if not isinstance(bases, dict):
            return {}
        return {
            tag: pair
            for tag, pair in bases.items()
            if isinstance(pair, list)
            and len(pair) == 2
            and all(isinstance(c, str) and c for c in pair)
        }
    return {}


def base_for(bases: dict[str, list[str]], tag: str, release: str | None) -> str | None:
    """The render commit stored for `tag`, while its release record still names `release`."""
    pair = bases.get(tag)
    return pair[0] if pair and release and pair[1] == release else None


def with_bases(
    owed: str | None, subject: str, bases: dict[str, list[str]]
) -> str | None:
    """`owed` with `bases` on `subject`'s line, or None when there is no line or no change."""
    lines = owed.splitlines() if owed else []
    for i, line in enumerate(lines):
        obj = _line_obj(line, subject)
        if obj is None:
            continue
        if obj.get(BASES_KEY) == bases:
            return None
        obj[BASES_KEY] = bases
        lines[i] = json.dumps(obj, sort_keys=True)
        return "\n".join(lines)
    return None


def wait_reason(newest: str | None, reached: bool) -> str:
    """Why a caller holds the line, from its newest render's commit, matching or not.

    Args:
        newest: that commit, or None for a caller with no render record.
        reached: whether `newest` descends from the line's origin.
    """
    if not newest:
        return NO_RENDER
    return NO_MATCH if reached else NOT_RENDERED


def log_waits(subject: str, origin: str, waits: dict[str, list[str]]) -> None:
    """Name the callers that still hold `subject`'s line at `origin`, by reason."""
    if not waits:
        return
    parts = [
        f"{len(tags)} {reason}: {', '.join(sorted(tags))}"
        for reason, tags in sorted(waits.items())
    ]
    hint = (
        " — waiting on the hourly render producer"
        if set(waits) == {NOT_RENDERED}
        else ""
    )
    log(f"k8s_unapplied: {subject} waits at {origin[:8]}: {'; '.join(parts)}{hint}")
