# ansible/roles/setup/gitops_deploy/files/gitops_ledger.py
"""The JSON-lines markers: the owed-work ledger (#3392) and the per-SHA tick receipt (#3391).

`gitops_markers` holds the line-format markers, whose parsers accept an exact field count and
skip any other line. Their reader copies redeploy on their own schedules, so a field a new
writer appended read as no pending work in a reader that had not redeployed. That is why
`manual_plane` once needed a `manual_plane_tags` sidecar and `k8s_unapplied` was its own
file. Every reader here
ignores the keys it does not know, and every writer carries them through a rewrite, so a new
key is invisible to an old reader instead of erasing the line.

Shipped with the deployer (`tasks/code.yml`) and imported from the checkout through the same
`sys.path` insert as `gitops_markers`. monitor-bridge carries a copy beside its `gitops_markers`
copy, written by `scripts/dev/gen_gitops_markers.py`, because it pages on `manual_plane` and
`k8s_deferred` and names the `hold_plane` entries: a class it reads can move into the ledger
only once that copy reads it. deploy-ui installs this source beside its `gitops_markers`, for
its `k8s_deferred` panel and its hold panel and Clear. renovate-agent installs it for the
plane its hold skip names.

Stdlib only, plus `gitops_markers`.
"""

import json
from typing import NamedTuple

from gitops_markers import (
    NO_PLAYBOOK,
    K8sDeferredEntry,
    ManualPlaneEntry,
)

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

# A setup role a tick fast-forwarded past and cannot apply itself. The subject is the role,
# under the `--tags` value that selects it. Two keys are this class's own: `playbook`, the
# playbook that applies the role or `NO_PLAYBOOK`, and `tags`, the narrowest `--tags` values
# the change needs, where an empty list means the whole role.
#
# It replaced the `manual_plane` line marker and its `manual_plane_tags` sidecar (#3392). The
# readers moved first, then the writer, and the line-marker readers went in #3487 once no host
# held a legacy file. A line missing either class key still pages, under the default
# `manual_plane_entries` gives it.
OWED_MANUAL_PLANE = "manual_plane"

# A promoted image bump a BROAD tick fast-forwarded and ran out of budget to deploy. The
# subject is the service, under the `--tags` value that selects it. monitor-bridge pages on it
# past its age gate, so it moved the way `manual_plane` did: every reader learned the class
# before the writer recorded it, and the line-marker readers and the fold of its last lines
# went once daniel-box held no legacy file (#3392).
#
# Unlike `k8s_unapplied`, a line KEEPS its recorded origin: the deployer clears it on the
# apply that covers it, so nothing needs the origin to advance.
OWED_K8S_DEFERRED = "k8s_deferred"

# A broad apply that failed and holds the deployer. The subject is the whole entry text
# `deploy_git.hold_plane_marker` writes, `<playbook> <tags>`, rather than the playbook alone:
# two failed applies of one playbook with different tags are two entries, and each clears
# on its own apply. Keyed on the playbook, `drop_owed` would erase both on the first apply,
# which is #878's erasure over again. `hold_sha` still decides whether anything pages; this
# class only says which planes the hold is waiting on.
#
# It moved the way `k8s_deferred` did (#3392): every reader learned the class before the
# writer recorded it, and the line-marker half of `held_planes` and the fold of its last
# entries went once daniel-box held no legacy `hold_plane` file.
OWED_HOLD_PLANE = "hold_plane"

# Every class a ledger line may carry. A reader asks for its classes by name and never sees
# the rest, which is what lets a new class ship before every reader knows it.
OWED_CLASSES: frozenset[str] = frozenset(
    {OWED_HOLD_PLANE, OWED_K8S_DEFERRED, OWED_K8S_UNAPPLIED, OWED_MANUAL_PLANE}
)


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


def _owed_objects(text: str | None, cls: str | None) -> list[tuple[OwedEntry, dict]]:
    """Every readable ledger line as its entry and its whole decoded object, for `parse_owed`.

    The object carries the keys beyond the four, which a class with keys of its own reads.
    """
    out = []
    for line in (text or "").splitlines():
        obj = _json_object(line)
        entry = _owed_entry(obj)
        if entry is not None and obj is not None and (cls is None or entry.cls == cls):
            out.append((entry, obj))
    return out


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
    return [entry for entry, _ in _owed_objects(text, cls)]


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

    Every key a line carries beyond the four survives the rewrite, because a newer writer may
    have put it there.

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


def k8s_deferred_entries(owed: str | None) -> list[K8sDeferredEntry]:
    """Every pending deferred image bump in the `owed` ledger, oldest first.

    One entry per service. A service on two lines keeps the OLDER one, its origin included,
    because the age gate dates the page from the first deferral.
    """
    oldest: dict[str, K8sDeferredEntry] = {}
    for e in parse_owed(owed, OWED_K8S_DEFERRED):
        held = oldest.get(e.subject)
        if held is None or e.at < held.at:
            oldest[e.subject] = K8sDeferredEntry(e.origin, e.subject, e.at)
    return sorted(oldest.values(), key=lambda e: e.at)


def held_planes(owed: str | None) -> list[str]:
    """Every plane a hold waits on: each `hold_plane` ledger subject, oldest first.

    Args:
        owed: the `owed` ledger's text, or None.

    A subject on two lines is one plane, at its first recorded place.
    """
    planes: list[str] = []
    for e in sorted(parse_owed(owed, OWED_HOLD_PLANE), key=lambda e: e.at):
        if e.subject.strip() not in planes:
            planes.append(e.subject.strip())
    return planes


def _token(value) -> str | None:
    """`value` where it is a non-empty string with no whitespace, else None.

    A role, playbook or tag is one word in every place it is printed: the `--tags` value and
    the playbook path.
    """
    if isinstance(value, str) and value and not any(c.isspace() for c in value):
        return value
    return None


def _manual_plane_tags(obj: dict) -> frozenset[str]:
    """The `tags` key of a `manual_plane` line. Empty means the whole role.

    A missing or malformed key reads as the whole role, never as a skipped line. A narrower
    answer from a torn key would print a clear that drops work nobody applied (#2349).
    """
    tags = obj.get("tags")
    if not isinstance(tags, list):
        return frozenset()
    words = [_token(t) for t in tags]
    if any(w is None for w in words):
        return frozenset()
    return frozenset(w for w in words if w is not None)


def manual_plane_entries(
    owed: str | None,
) -> tuple[list[ManualPlaneEntry], dict[str, frozenset[str]]]:
    """Every pending `manual_plane` role in the `owed` ledger, and the tags each needs.

    Returns:
        The entries, oldest first, and role -> the narrowest tags its change needs, where an
        empty frozenset means the whole role. Every role in the first has a row in the second.

    A line whose `playbook` is missing or malformed STILL PAGES, as `NO_PLAYBOOK`: the page
    then says to apply the role by hand rather than naming a playbook. Skipping it would
    silence the page, the failure this ledger exists to remove. A subject that is not one
    word names no role, and its line is skipped as torn.
    """
    entries, tags = [], {}
    for entry, obj in _owed_objects(owed, OWED_MANUAL_PLANE):
        role = _token(entry.subject)
        if role is None:
            continue
        playbook = _token(obj.get("playbook")) or NO_PLAYBOOK
        entries.append(ManualPlaneEntry(entry.origin, playbook, role, entry.at))
        tags[role] = _manual_plane_tags(obj)
    return sorted(entries, key=lambda e: e.at), tags


def put_manual_plane(
    owed: str | None, entry: ManualPlaneEntry, tags: frozenset[str]
) -> str:
    """`owed` with `entry.role`'s `manual_plane` line set to `entry` and `tags`.

    The line is rewritten where it stands and appended where the role has none. Every key it
    carries beyond the class's own survives, for the reason `rewrite_owed` keeps them. A
    second line for the same role, readable or torn, is dropped: the first stands for it, and
    two would page twice for one change.
    """
    own = ("class", "subject", "origin", "at", "playbook", "tags")
    lines, placed = [], False
    for line in (owed or "").splitlines():
        if owed_line_key(line) != (OWED_MANUAL_PLANE, entry.role):
            lines.append(line)
            continue
        if placed:
            continue
        extra = {k: v for k, v in (_json_object(line) or {}).items() if k not in own}
        lines.append(_manual_plane_line(entry, tags, extra))
        placed = True
    if not placed:
        lines.append(_manual_plane_line(entry, tags, {}))
    return "\n".join(lines)


def _manual_plane_line(entry: ManualPlaneEntry, tags: frozenset[str], extra) -> str:
    """One `manual_plane` ledger line, the reverse of `manual_plane_entries` for one role."""
    return owed_line(
        OWED_MANUAL_PLANE,
        entry.role,
        entry.origin,
        entry.at,
        **extra,
        playbook=entry.playbook,
        tags=sorted(tags),
    )


# ── the per-SHA tick receipt (#3391)─────────────────────────────────────────────────────

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
