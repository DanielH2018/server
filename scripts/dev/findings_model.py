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
from datetime import UTC, datetime

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

# THIS REPOSITORY IS PUBLIC, so any GitHub account can comment on a `claude`-labelled issue.
# A claim trailer is therefore only trusted from the operator: without this check a drive-by
# comment reading ``Released: `<branch>` `` closed a live claim — handing an issue somebody
# was working to a second session — and one reading ``Claim: `<any live branch>` `` withheld
# an issue from `next` and made `claim` refuse it for as long as that branch existed (#1280).
#
# TWO SIGNALS, EITHER SUFFICES. `viewerDidAuthor` is true for the account gh is authenticated
# as, which is the operator in a session and in the docs-refresh cron alike. The association
# half keeps working if that account ever changes, since a claim the operator posted from
# another of their machines still reads OWNER.
_TRUSTED_ASSOCIATIONS = frozenset(("OWNER", "MEMBER", "COLLABORATOR"))


def is_operator_comment(comment: dict) -> bool:
    """Whether ``comment``'s claim trailer counts, from the author fields gh returns.

    FAIL-CLOSED: a comment carrying neither field folds into nothing. gh populates both on
    every comment — on `issue list` as well as `issue view`, verified against this repo — so
    the missing case is a fixture that did not stamp authorship rather than a real comment,
    and a comment nothing can attribute must not decide who holds an issue.
    """
    if comment.get("viewerDidAuthor"):
        return True
    return (comment.get("authorAssociation") or "") in _TRUSTED_ASSOCIATIONS


def _one_line(text: str) -> str:
    """``text`` with every line break collapsed to a space.

    THE PROSE ABOVE A TRAILER IS PARSED TOO (#1284). `current_claim` tests `_CLAIM_RE` before
    `_RELEASE_RE` on the WHOLE comment body, and `release_comment` puts the reason above the
    trailer — so a reason carrying a line ``Claim: `x` `` makes a release read as a claim by
    `x`. That is not a hypothetical hand-typed `--reason`: `cmd_reap` builds its own reason
    from `classify`'s text, which embeds a worktree's `lock_reason`, which is free text
    nobody in this repo writes.

    Collapsing the breaks closes it at the builder rather than at argparse, which is where
    the named vector arrives — `_CLAIM_RE` is anchored with `re.M`, so a trailer that cannot
    start a line cannot match at all. Anchoring the parser to the body's last trailer line
    was the alternative and is rejected: it inverts the DECIDED marker in `current_claim`
    that makes a `Claim:` line win over a `Released:` one, which points the other way on
    purpose.
    """
    return " ".join((text or "").splitlines())


def validate_worktree_name(name: str) -> str | None:
    """Why ``name`` cannot be carried by a claim trailer, or None when it can.

    `_CLAIM_RE` captures `[^`\\n]+` between backticks, so a name holding a backtick or a line
    break writes a comment and a label and then fails to parse its own trailer on read-back —
    and `cmd_claim` reported `lost the race to \\`None\\``, telling the operator they lost a
    race that never happened (#1284). Refusing the name up front is the fix; the read-back
    message below it is the second half, for a read that comes back empty for any other
    reason.
    """
    if not name.strip():
        return "empty"
    if name != name.strip():
        return "has leading or trailing whitespace"
    if "`" in name:
        return "contains a backtick, which ends the claim trailer's own quoting"
    if any(c in name for c in "\r\n"):
        return "contains a line break, so the claim trailer would not parse"
    return None


def now_iso() -> str:
    """The `when` every claim and release comment is stamped with.

    Lives beside the two comment builders that consume it: `findings.py` and
    `findings_claim_cli.py` both stamp comments, and a private `_now` in one of them would
    have to be imported across a CLI boundary by the other.
    """
    return datetime.now(UTC).isoformat()


def claim_comment(worktree: str, session: str | None, when: str) -> str:
    """The comment body that claims an issue for ``worktree``.

    Args:
        worktree: the git worktree doing the work; also the claim's identity.
        session: the Claude session id, when one is known. Prose only — nothing parses it.
        when: an ISO-8601 timestamp, for a human reading the thread.
    """
    who = f" (session `{_one_line(session)}`)" if session else ""
    return f"Claimed by `{worktree}`{who} at {when}\n\nClaim: `{worktree}`\n"


def release_comment(worktree: str, when: str, reason: str | None) -> str:
    """The comment body that releases ``worktree``'s claim.

    The reason is collapsed to one line — see `_one_line` for why a multi-line reason turns
    a release into a claim by whoever the reason names (#1284).
    """
    why = f" — {_one_line(reason)}" if reason else ""
    return f"Released by `{worktree}` at {when}{why}\n\nReleased: `{worktree}`\n"


# What `gh issue view --json comments` returns at most: its GraphQL query asks for
# `comments(first: 100)` and nothing paginates. Past this the fold stops seeing new
# trailers — a release at #101 would leave the issue claimed forever, and a claim at #101
# would make `cmd_claim`'s read-back report a race against nobody (#1284). Latent while the
# busiest issue in the register carries a handful of comments, so this WARNS rather than
# changing the read: a wrong claim verdict that announces itself is the point.
COMMENT_PAGE_CAP = 100


def comment_cap_warning(issue: dict) -> str | None:
    """A warning when ``issue`` carries as many comments as gh will return, else None.

    Pure, so `findings_gh` decides where it is printed and the rule itself is testable
    without a boundary.
    """
    n = len(issue.get("comments", []))
    if n < COMMENT_PAGE_CAP:
        return None
    return (
        f"warning: #{issue.get('number')} has {n} comments, gh's page cap — a claim or "
        "release past the cap is invisible to the fold, so its claim verdict may be wrong"
    )


def ordered_comments(issue: dict) -> list[dict]:
    """``issue``'s comments in `createdAt` order, or in gh's own order when it cannot say.

    THE ORDER IS THE PROTOCOL. `current_claim` folds forward and implements FIRST WRITER
    WINS, so the fold's answer is only correct if the list really is oldest-first. gh returns
    them that way — measured ascending across 17 issues — but nothing enforced it (#1284).

    SORTS ONLY WHEN EVERY COMMENT CARRIES A TIMESTAMP. A mixed list is the dangerous case: a
    key of `c.get("createdAt") or ""` hoists every unstamped comment to the front, which can
    flip the verdict of the one function in this protocol that must not change by accident.
    Fixtures routinely omit `createdAt`, so the mixed list is not hypothetical. When any
    comment lacks one, gh's own order stands — exactly the behaviour that shipped before.

    `current_claim` and `_claim_age_days` in findings_claim.py carry the identical fold and
    must consume the identical order, or a `claims` row ages a claim the register does not
    think exists.
    """
    comments = list(issue.get("comments", []))
    if not all(c.get("createdAt") for c in comments):
        return comments
    return sorted(comments, key=lambda c: c["createdAt"])


def current_claim(issue: dict) -> str | None:
    """The worktree currently holding ``issue``, or None.

    Folds the comment list forward oldest-first (see `ordered_comments`): a ``Claim:`` line
    opens IF nothing is currently held, and a ``Released:`` line naming the SAME worktree
    closes.

    FIRST WRITER WINS, not last. gh returns comments in createdAt order, so the earlier of
    two racing claims is the earlier comment. Letting a later claim overwrite a live one
    would mean a session could take an issue out from under another by claiming it again,
    and — worse — the first claimant's own ``release`` would then be refused, so it could
    not even clean up after losing.

    A release naming some other worktree is ignored, so one session cannot release
    another's claim by accident.

    Only the OPERATOR's comments are folded — see `is_operator_comment`. This repo is public,
    so a comment from any other account is prose on the thread and nothing more.
    """
    held: str | None = None
    for comment in ordered_comments(issue):
        if not is_operator_comment(comment):
            continue
        body = comment.get("body") or ""
        claimed = _CLAIM_RE.search(body)
        if claimed:
            if held is None:
                held = claimed.group(1)
            # DECIDED: `Claim:` wins over `Released:` within ONE comment body. Neither
            # `claim_comment` nor `release_comment` ever emits both trailers, so a body
            # carrying both is malformed input, and every fail-safe in this module points
            # the same way: `is_operator_comment` folds an unattributable comment into
            # nothing, `claim_is_live` HOLDS a claim whose worktree it cannot read,
            # `cmd_reap` refuses on a git error, and `cmd_next` withholds. Releasing on
            # ambiguity is the one direction that hands live work to a second session,
            # which is the harm this protocol exists to prevent. This `continue` is what
            # implements the choice, so it is not a tidy-up — dropping it inverts the
            # verdict. `_claim_age_days` in findings_claim.py carries the identical fold
            # and must keep the identical skip, or a `claims` row ages a claim the
            # register does not think exists.
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


def pickable(
    issues: list[dict], *, live_claims: set[int], pr_refs: set[int]
) -> list[dict]:
    """The issues a session may pick up, best first.

    Args:
        live_claims: issue numbers whose claim is still live. A STALE claim does not
            withhold an issue — `cmd_claim` reaps one on its way past, and `reap` clears
            every one of them at once.
        pr_refs: issue numbers an open PR already says it closes. Without this, a session
            picks up work another session has finished but not yet landed.
    """
    rows = [
        r
        for r in issue_rows(issues)
        if not r["manual"]
        and r["number"] not in live_claims
        and r["number"] not in pr_refs
    ]
    return sorted(rows, key=sort_key)


def sort_key(row: dict) -> tuple:
    sev = (
        SEVERITIES.index(row["severity"])
        if row["severity"] in SEVERITIES
        else len(SEVERITIES)
    )
    return (sev, 0 if row["escalated"] else 1, row["number"])
