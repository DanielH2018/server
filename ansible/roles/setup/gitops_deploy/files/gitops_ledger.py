# ansible/roles/setup/gitops_deploy/files/gitops_ledger.py
"""The JSON-lines markers: the owed-work ledger (#3392) and the per-SHA tick receipt (#3391).

`gitops_markers` holds the line-format markers, whose parsers accept an exact field count and
skip any other line. Their reader copies redeploy on their own schedules, so a field a new
writer appended read as no pending work in a reader that had not redeployed. That is why
`manual_plane_tags` is a sidecar and `k8s_unapplied` was its own file. Every reader here
ignores the keys it does not know, and every writer carries them through a rewrite, so a new
key is invisible to an old reader instead of erasing the line.

Shipped with the deployer (`tasks/code.yml`) and imported from the checkout through the same
`sys.path` insert as `gitops_markers`. monitor-bridge reads neither marker and carries no copy
yet; the class it pages on moves only after one ships.

Stdlib only, plus `gitops_markers`.
"""

import json
from typing import NamedTuple

from gitops_markers import K8sDeferredEntry

# ── the owed-work ledger (#3392) ─────────────────────────────────────────────────────────

# A k8s role change — hand-edited or denylisted — that a tick fast-forwarded and will never
# apply. The subject is the service, under the `--tags` value that selects it.
#
# NOTHING PAGES ON THIS CLASS, by construction rather than by omission (#2570): forty of the
# fifty-four k8s roles are denylisted (`docs/reference/decisions.md`), so a page on their
# ordinary changes would hold GitOps Deploy — Status red as normal operation. Its readers are
# the SessionStart banner and the deployer's own journal, both read by a person who is
# already looking.
#
# THE ENTRY DISCHARGES ITSELF. A denylisted role is one this deployer never applies, so an
# entry with only a deployer-side clear would accumulate one per routine landing. Every tick
# asks, per entry, whether the service's release record
# (`roles/k8s/manifests/tasks/release_stamp.yml`) names a commit that CONTAINS the recorded
# SHA (`deploy_defer.discharge_k8s_unapplied`), which discharges an operator's own
# `deploy.sh` too.
OWED_K8S_UNAPPLIED = "k8s_unapplied"

# Every class a ledger line may carry. A reader asks for its classes by name and never sees
# the rest, which is what lets a new class ship before every reader knows it.
OWED_CLASSES: frozenset[str] = frozenset({OWED_K8S_UNAPPLIED})


class OwedEntry(NamedTuple):
    """One readable line of the `owed` ledger.

    Attributes:
        cls: the class, one of `OWED_CLASSES` from a writer this tree knows.
        subject: what is owed, under the `--tags` value that selects it.
        origin: the origin SHA the work was recorded at.
        at: when it was first recorded, in `time.time()` terms. The age every reader dates
            the work from, so a rewrite never refreshes it.
    """

    cls: str
    subject: str
    origin: str
    at: float


def _json_object(line: str) -> dict | None:
    """`line` as a JSON object, or None for anything else — blank, torn, or not an object."""
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _owed_entry(obj: dict | None) -> OwedEntry | None:
    """The entry one decoded ledger line carries, or None where a required key is missing.

    Every key beyond the four is IGNORED rather than refused. That is the property the ledger
    exists for: the line formats above skip a line with one field too many, so a writer that
    added a field silenced every reader that had not redeployed yet.
    """
    if obj is None:
        return None
    cls, subject, origin, at = (
        obj.get(k) for k in ("class", "subject", "origin", "at")
    )
    if not (
        isinstance(cls, str) and isinstance(subject, str) and isinstance(origin, str)
    ):
        return None
    if not (cls and subject and origin):
        return None
    if isinstance(at, bool) or not isinstance(at, (int, float)):
        return None
    return OwedEntry(cls, subject, origin, float(at))


def parse_owed(text: str | None, cls: str | None = None) -> list[OwedEntry]:
    """Every readable line of the `owed` ledger, in the order they stand.

    Args:
        text: the ledger's contents, or None for an absent ledger.
        cls: keep only this class. None keeps every class, including ones this tree has never
            heard of.

    A line that is not a JSON object, or lacks one of `class`, `subject`, `origin` and `at`, is
    SKIPPED, for the reason every parser here skips one: a page raised off a torn line names
    nothing and cannot be cleared. The writers carry such a line through untouched.
    """
    entries = []
    for line in (text or "").splitlines():
        entry = _owed_entry(_json_object(line))
        if entry is not None and (cls is None or entry.cls == cls):
            entries.append(entry)
    return entries


def owed_line(cls: str, subject: str, origin: str, at: float, **extra) -> str:
    """One ledger line. Keys sorted, so two writers recording the same entry write one string."""
    return json.dumps(
        {**extra, "class": cls, "subject": subject, "origin": origin, "at": int(at)},
        sort_keys=True,
    )


def owed_line_key(line: str) -> tuple[str, str] | None:
    """The `(class, subject)` a RAW ledger line names, or None where it names nothing.

    A line missing its `origin` or `at` is still ATTRIBUTABLE, and a writer repairs it rather
    than appending a second line beside it (#2657). A line naming no class and subject is
    carried untouched by every writer: dropping it loses the only record that something was
    owed, and nothing can say what.
    """
    obj = _json_object(line)
    if obj is None:
        return None
    cls, subject = obj.get("class"), obj.get("subject")
    if isinstance(cls, str) and cls and isinstance(subject, str) and subject:
        return cls, subject
    return None


def rewrite_owed(
    text: str | None, cls: str, subjects, origin: str, now: float, advance: bool
) -> str:
    """`text` with every `cls` line naming one of `subjects` brought up to date.

    The ledger's form of `rewrite_k8s_lines`, with one difference: every key a line carries
    beyond the four survives the rewrite, because a newer writer may have put it there.

    Two rewrites, and both keep the line's first-seen stamp. A TORN LINE IS MADE READABLE
    (#2657), taking its own `at` where that reads as a number and `now` where it does not.
    Under `advance`, a readable line moves to `origin` (#2644). A torn line BESIDE a readable
    one for the same subject is dropped, since repairing it would duplicate that line.

    Returns:
        The text, rewritten. Compare it with the original to see whether anything changed.
    """
    wanted = set(subjects)
    readable = {e.subject for e in parse_owed(text, cls)}
    kept = []
    for line in (text or "").splitlines():
        key = owed_line_key(line)
        if key is None or key[0] != cls or key[1] not in wanted:
            kept.append(line)
            continue
        obj = _json_object(line) or {}
        entry = _owed_entry(obj)
        if entry is not None:
            if advance and entry.origin != origin:
                obj["origin"] = origin
                kept.append(json.dumps(obj, sort_keys=True))
            else:
                kept.append(line)
        elif key[1] not in readable:
            readable.add(key[1])
            at = obj.get("at")
            stamp = (
                at if isinstance(at, (int, float)) and not isinstance(at, bool) else now
            )
            first = obj.get("origin")
            if advance or not (isinstance(first, str) and first):
                first = origin
            extra = {
                k: v
                for k, v in obj.items()
                if k not in ("class", "subject", "origin", "at")
            }
            kept.append(owed_line(cls, key[1], first, stamp, **extra))
    return "\n".join(kept)


def drop_owed(text: str | None, cls: str, subjects) -> tuple[str, list[str]]:
    """`text` without its `cls` lines naming any of `subjects`, and the subjects dropped.

    A TORN line naming one of `subjects` goes too (#2657); a line naming nobody stays, for the
    reason `owed_line_key` gives.
    """
    wanted = set(subjects)
    kept, dropped = [], set()
    for line in (text or "").splitlines():
        key = owed_line_key(line)
        if key is not None and key[0] == cls and key[1] in wanted:
            dropped.add(key[1])
            continue
        kept.append(line)
    return "\n".join(kept), sorted(dropped)


def k8s_unapplied_entries(owed: str | None) -> list[K8sDeferredEntry]:
    """Every pending `k8s_unapplied` change in the `owed` ledger.

    Returned as `K8sDeferredEntry` because every caller reads `.service`, `.origin` and `.at`
    off it.
    """
    return [
        K8sDeferredEntry(e.origin, e.subject, e.at)
        for e in parse_owed(owed, OWED_K8S_UNAPPLIED)
    ]


# ── the per-SHA tick receipt (#3391) ─────────────────────────────────────────────────────

# How many receipts the `receipts` marker keeps. A landing reads the receipt for its own
# merge commit within one or two ticks, and a broad range is a few a day at most, so this is
# days of history in a file of a few kilobytes.
RECEIPT_KEEP = 50


class Receipt(NamedTuple):
    """What one tick did with one origin SHA's broad change.

    Attributes:
        origin: the origin SHA the tick crossed to.
        base: the commit the checkout stood on before it. The range is `base..origin`.
        applied: playbook -> the `--tags` it applied with, for each plane the tick APPLIED.
            An empty tuple is a whole-playbook apply.
        manual: setup role tag -> the narrowest tags its change in this range needs, for
            each role the tick left owed to a hand. An empty frozenset means no derivation
            could narrow it, so the reader prints the whole-role tag.
    """

    origin: str
    base: str
    applied: dict[str, tuple[str, ...]]
    manual: dict[str, frozenset[str]]


def _receipt(obj: dict | None) -> Receipt | None:
    """The receipt one decoded line carries, or None where its shape is not a receipt.

    Unknown keys are ignored, as `parse_owed` ignores them, and for the same reason.
    """
    if obj is None:
        return None
    origin, base = obj.get("origin"), obj.get("base")
    applied, manual = obj.get("applied", {}), obj.get("manual", {})
    if not (isinstance(origin, str) and origin and isinstance(base, str)):
        return None
    if not (isinstance(applied, dict) and isinstance(manual, dict)):
        return None
    if not all(isinstance(v, list) for v in [*applied.values(), *manual.values()]):
        return None
    return Receipt(
        origin,
        base,
        {k: tuple(str(t) for t in v) for k, v in applied.items()},
        {k: frozenset(str(t) for t in v) for k, v in manual.items()},
    )


def parse_receipts(text: str | None) -> list[Receipt]:
    """Every readable receipt in the `receipts` marker, oldest first. Torn lines are skipped."""
    out = []
    for line in (text or "").splitlines():
        receipt = _receipt(_json_object(line))
        if receipt is not None:
            out.append(receipt)
    return out


def receipt_line(receipt: Receipt) -> str:
    """One `receipts` line, the reverse of `parse_receipts` for a single receipt."""
    return json.dumps(
        {
            "origin": receipt.origin,
            "base": receipt.base,
            "applied": {k: list(v) for k, v in sorted(receipt.applied.items())},
            "manual": {k: sorted(v) for k, v in sorted(receipt.manual.items())},
        },
        sort_keys=True,
    )
