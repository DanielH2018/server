"""The finding vocabulary and the pure reads over a gh issue: no gh, no shell, no argv.

One issue per fingerprint is the rule that makes the register a register, so the fingerprint
and the trailer that carries it live here rather than beside the code that files them. The
same goes for the `## Verify-by` section: `findings.py open` writes it and `findings.py
verify` reads it back, and neither owns the format.

Everything here takes plain values and returns plain values. `findings_plans.py` turns these
answers into gh argv, `findings_gh.py` runs them, and `findings.py` is the CLI over both.
"""

import hashlib
import re

SEVERITIES = ("high", "medium", "low")
KINDS = ("gap", "improvement", "addition")
DOMAINS = (
    "backup-observability",
    "cicd",
    "container",
    "docs",
    "network",
    "security",
    "home-assistant",
)

# The two closes nothing reopens. A finding closes as `fixed` (completed), or as one of these
# two, which close as not planned and carry a label of the same name: `refuted` means a
# skeptic disproved it, `accepted` means it is TRUE and the operator chose to live with it.
# `plan_open` returning early on both is the whole point — without `accepted`, an accepted
# trade-off had to be closed by hand, and the next review re-filed it as a regression.
NO_REOPEN = frozenset(("refuted", "accepted"))

# name -> (colour, description). Colours are Catppuccin Mocha so the label set reads as one
# system with the artifacts; severity is red / yellow / green, state markers are greys.
LABELS: dict[str, tuple[str, str]] = {
    "claude": ("cba6f7", "Filed by Claude via scripts/dev/findings.py"),
    "severity/high": ("f38ba8", "Review severity: High"),
    "severity/medium": ("f9e2af", "Review severity: Medium"),
    "severity/low": ("a6e3a1", "Review severity: Low"),
    "kind/gap": ("fab387", "Something missing that should exist"),
    "kind/improvement": ("89b4fa", "Something present that could be better"),
    "kind/addition": ("94e2d5", "A new capability"),
    "refuted": (
        "6c7086",
        "Closed because a skeptic disproved the finding; do not reopen",
    ),
    "accepted": (
        "9399b2",
        "Closed because the operator accepted the trade-off; true, but do not reopen",
    ),
    "escalated": ("f5c2e7", "Re-observed three or more times; needs a durable owner"),
    "no-vetted-remediation": ("585b70", "Every proposed fix failed the fix-skeptic"),
    "manual": (
        "6c7086",
        "Reserved for the operator; no Claude session claims or fans this out",
    ),
    "claimed": (
        "9399b2",
        "A session is working this issue; see the newest Claim: comment",
    ),
}
for _d in DOMAINS:
    LABELS[f"domain/{_d}"] = ("b4befe", f"Reviewer domain: {_d}")

# The GitHub Project (v2) every created issue is added to. Projects is a board over issues,
# not a second record: the issue and its labels stay the source of truth, and the board is
# what the operator looks at. `gh issue create --project <title>` adds the item in the same
# call, so no second write and no item id. It needs the `project` token scope, so the board
# is best-effort: `cmd_open` retries without it and warns rather than losing the finding.
PROJECT_TITLE = "Claude findings"

# DECIDED: the underscore-prefixed names below, and `_prefixed` further down, cross a module
# boundary on purpose. They moved out of findings.py byte-for-byte, changing no behaviour;
# renaming them would have turned that move into a rewrite. The underscore still carries what
# it did before — internal to the findings modules, not an API another script may import.
# Conventions for a new module: docs/python-code-organization.md.
_LIST_FIELDS = "number,title,state,labels,body,createdAt,closedAt,url,comments"
# `\s*$` rather than `$`: a body fetched from the API can carry CRLF line endings, and
# `re.M`'s `$` matches before the `\n` but not before the `\r`.
_FP_RE = re.compile(r"^Fingerprint: `([0-9a-f]{12})`\s*$", re.M)
_LINE_SUFFIX = re.compile(r":\d+(?:-\d+)?$")
_REOBSERVED = "Re-observed"
# DOTALL so `.` crosses the command's own newlines; the heading anchors it against prose
# a human added elsewhere in the body, and the fence is stripped by the capture group.
_VERIFY_BY_RE = re.compile(r"^## Verify-by\s*\n```[^\n]*\n(.*?)\n```", re.M | re.S)


def fingerprint(title: str, file: str | None) -> str:
    """Stable id for a finding: title words plus the file, never the line number."""
    norm_title = " ".join(re.sub(r"[^0-9a-z]+", " ", title.lower()).split())
    norm_file = _LINE_SUFFIX.sub("", (file or "").strip())
    return hashlib.sha1(f"{norm_title}\n{norm_file}".encode()).hexdigest()[:12]


def trailer(fp: str, source: str) -> str:
    return (
        "\n\n---\n"
        f"Fingerprint: `{fp}`\n"
        f"Source: {source}\n"
        "Filed by `scripts/dev/findings.py`. Re-observations are comments beginning "
        f"`{_REOBSERVED}`.\n"
    )


def verify_by_section(command: str) -> str:
    """The `## Verify-by` body section `open --verify-by` appends before the trailer."""
    return f"\n\n## Verify-by\n```\n{command}\n```\n"


def parse_verify_by(body: str) -> str | None:
    """The verify-by command stored in an issue body, or None.

    Reads the `## Verify-by` heading and its fenced code block back out, the one format
    `verify_by_section` writes, so a body a human has edited around still parses. A body
    fetched from the API can carry CRLF line endings, so they are normalized first.
    """
    m = _VERIFY_BY_RE.search((body or "").replace("\r\n", "\n"))
    if not m:
        return None
    cmd = m.group(1).strip()
    return cmd or None


def label_names(issue: dict) -> set[str]:
    return {lab["name"] for lab in issue.get("labels", [])}


def find_by_fingerprint(issues: list[dict], fp: str) -> dict | None:
    for issue in issues:
        m = _FP_RE.search(issue.get("body") or "")
        if m and m.group(1) == fp:
            return issue
    return None


def reobservations(issue: dict) -> int:
    return sum(
        1
        for c in issue.get("comments", [])
        if (c.get("body") or "").startswith(_REOBSERVED)
    )


# A claim is an append-only comment, not a body edit or an assignee. `gh` authenticates as
# one account, so an assignee names the operator rather than the session; and two sessions
# editing a body race, where two sessions commenting both succeed and gh returns them in
# createdAt order. Folding that ordered list forward implements FIRST WRITER WINS: a later
# claim cannot overwrite a live one, so one session cannot steal an issue from another, and
# the first claimant can always clean up by releasing its own claim.
_CLAIM_RE = re.compile(r"^Claim: `([^`\n]+)`\s*$", re.M)
_RELEASE_RE = re.compile(r"^Released: `([^`\n]+)`\s*$", re.M)


def claim_comment(worktree: str, session: str | None, when: str) -> str:
    """The comment body that claims an issue for ``worktree``.

    Args:
        worktree: the git worktree doing the work; also the claim's identity.
        session: the Claude session id, when one is known. Prose only — nothing parses it.
        when: an ISO-8601 timestamp, for a human reading the thread.
    """
    who = f" (session `{session}`)" if session else ""
    return f"Claimed by `{worktree}`{who} at {when}\n\nClaim: `{worktree}`\n"


def release_comment(worktree: str, when: str, reason: str | None) -> str:
    """The comment body that releases ``worktree``'s claim."""
    why = f" — {reason}" if reason else ""
    return f"Released by `{worktree}` at {when}{why}\n\nReleased: `{worktree}`\n"


def current_claim(issue: dict) -> str | None:
    """The worktree currently holding ``issue``, or None.

    Folds the comment list forward in the order gh returned it: a ``Claim:`` line opens IF
    nothing is currently held, and a ``Released:`` line naming the SAME worktree closes.

    FIRST WRITER WINS, not last. gh returns comments in createdAt order, so the earlier of
    two racing claims is the earlier comment. Letting a later claim overwrite a live one
    would mean a session could take an issue out from under another by claiming it again,
    and — worse — the first claimant's own ``release`` would then be refused, so it could
    not even clean up after losing.

    A release naming some other worktree is ignored, so one session cannot release
    another's claim by accident.
    """
    held: str | None = None
    for comment in issue.get("comments", []):
        body = comment.get("body") or ""
        claimed = _CLAIM_RE.search(body)
        if claimed:
            if held is None:
                held = claimed.group(1)
            continue
        released = _RELEASE_RE.search(body)
        if released and released.group(1) == held:
            held = None
    return held


# GitHub closes an issue on any of these, not just `Closes`. Matching `closes` alone would
# let `next` offer an issue whose fix is already open as a PR, which is the exact duplicated
# work `next` exists to prevent.
_PR_REF_RE = re.compile(r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)\b")


def pr_refs(bodies: list[str]) -> set[int]:
    """Issue numbers the given PR bodies say they close."""
    return {int(m) for body in bodies for m in _PR_REF_RE.findall(body or "")}


def _prefixed(names: set[str], prefix: str) -> str | None:
    """The first label under ``prefix``, alphabetically.

    Iterating the set directly would pick an arbitrary one of two `severity/*` labels, so
    the same issue could render two different rows on two runs.
    """
    for n in sorted(names):
        if n.startswith(prefix):
            return n[len(prefix) :]
    return None


def issue_rows(issues: list[dict]) -> list[dict]:
    """Flattens gh's issue JSON into the rows ``sort_key`` and ``cmd_list`` consume.

    Args:
        issues: gh issue objects, each carrying labels, comments and the other
            ``_LIST_FIELDS``.
    """
    rows = []
    for issue in issues:
        names = label_names(issue)
        rows.append(
            {
                "number": issue["number"],
                "title": issue["title"],
                "state": issue.get("state", "OPEN"),
                "severity": _prefixed(names, "severity/"),
                "kind": _prefixed(names, "kind/"),
                "domain": _prefixed(names, "domain/"),
                "escalated": "escalated" in names,
                "refuted": "refuted" in names,
                "accepted": "accepted" in names,
                "no_vetted_remediation": "no-vetted-remediation" in names,
                "verify_by": parse_verify_by(issue.get("body") or "") is not None,
                "manual": "manual" in names,
                "claimed": current_claim(issue),
                "first_seen": (issue.get("createdAt") or "")[:10],
                "reobservations": reobservations(issue),
                "url": issue.get("url", ""),
            }
        )
    return rows


def sort_key(row: dict) -> tuple:
    sev = (
        SEVERITIES.index(row["severity"])
        if row["severity"] in SEVERITIES
        else len(SEVERITIES)
    )
    return (sev, 0 if row["escalated"] else 1, row["number"])
