"""The finding vocabulary and the pure reads over a gh issue: no gh, no shell, no argv.

One issue per fingerprint is the rule that makes the register a register, so the fingerprint and
the trailer that carries it live here rather than beside the code that files them. The same goes
for the `## Verify-by` section: `findings.py open` writes it and `findings.py verify` reads it
back, and neither owns the format. That section holds prose — a description of how to check the
finding — and `parse_verify_by` also reads the fenced shell command that older bodies carry,
because those bodies are still in the register.

Everything here takes plain values and returns plain values. `findings_lib/plans.py` turns these
answers into gh argv, `findings_lib/gh_calls.py` runs them, and `findings.py` is the CLI over both.
"""

import hashlib
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum
from functools import cached_property

SEVERITIES = ("high", "medium", "low")
KINDS = ("gap", "improvement", "feature")
DOMAINS = (
    "backup-observability",
    "cicd",
    "container",
    "docs",
    "network",
    "security",
    "home-assistant",
)

# `refuted` and `accepted` close as not planned and are never reopened or re-filed.
NO_REOPEN = frozenset(("refuted", "accepted"))

LABELS: dict[str, tuple[str, str]] = {
    "claude": ("cba6f7", "Filed by Claude via scripts/dev/findings.py"),
    "refuted": (
        "6c7086",
        "Closed because a skeptic disproved the finding; do not reopen",
    ),
    "accepted": (
        "9399b2",
        "Closed because the operator accepted the trade-off; true, but do not reopen",
    ),
    "no-vetted-remediation": ("585b70", "Every proposed fix failed the fix-skeptic"),
    "manual": (
        "6c7086",
        "Reserved for the operator; no Claude session claims or fans this out",
    ),
    "review-leftover": ("fab387", "Left after a fan-out fix round; next ranks it last"),
}
SEVERITY_FIELD, NOT_BEFORE_FIELD = "Severity", "Not Before"
# The account-level issue field holding an issue's reviewer domain; its options are `DOMAINS`.
DOMAIN_FIELD = "Domain"
SEVERITY_OPTIONS = {s: s.capitalize() for s in SEVERITIES}
KIND_TYPES = {"gap": "Gap", "improvement": "Improvement", "feature": "Feature"}
# The labels that held the same two values. Sessions still on a pre-field checkout write only
# these, so the readers below take them as a fallback.
# TODO: https://github.com/DanielH2018/server/issues/9120 - delete with the labels.
NOT_BEFORE_PREFIX = "not-before:"
SEVERITY_PREFIX = "severity/"
KIND_PREFIX = "kind/"

# DECIDED: the underscore-prefixed names below, and `_prefixed` further down, cross a module
# boundary on purpose. They moved out of findings.py byte-for-byte, changing no behaviour;
# renaming them would have turned that move into a rewrite. The underscore still carries what
# it did before — internal to the findings modules, not an API another script may import.
# Conventions for a new module: docs/python-code-organization.md.
_LIST_FIELDS = "number,title,state,stateReason,labels,body,createdAt,closedAt,url,comments,blockedBy,author,milestone,issueType"
# Only this account's issues are work: any collaborator can open a `claude` issue, and its body
# would reach a session as instructions.
REGISTER_AUTHOR = "DanielH2018"


class IssueState(StrEnum):
    """An issue's state as gh's GraphQL spells it; REST's lower-case form is upper-cased."""

    OPEN = "OPEN"
    CLOSED = "CLOSED"


@dataclass(frozen=True)
class Comment:
    """One comment on an issue, with the author fields the claim protocol trusts.

    Attributes:
        body: the comment text.
        author: the commenter's login, or "" when gh returned none.
        association: gh's `authorAssociation`, such as `OWNER` or `NONE`.
        viewer_did_author: whether the account gh is authenticated as wrote it.
        created_at: gh's ISO-8601 `createdAt`, or "" when it carries none.
    """

    body: str
    author: str = ""
    association: str = ""
    viewer_did_author: bool = False
    created_at: str = ""

    @property
    def by_operator(self) -> bool:
        """Whether this comment's claim trailer counts, from the author fields gh returns.

        FAIL-CLOSED: a comment carrying neither field folds into nothing. gh populates both
        on every comment — on `issue list` as well as `issue view`, verified against this
        repo — so the missing case is a fixture that did not stamp authorship rather than a
        real comment, and a comment nothing can attribute must not decide who holds an issue.
        """
        return self.viewer_did_author or self.association in _TRUSTED_ASSOCIATIONS


@dataclass(frozen=True)
class Blocker:
    """One issue recorded as blocking another, from GitHub's "blocked by" relationship."""

    number: int
    state: IssueState
    url: str = ""


@dataclass(frozen=True)
class Claim:
    """The worktree holding an issue, and when the comment that opened its claim was posted.

    Attributes:
        worktree: the branch named in the `Claim:` trailer.
        opened_at: the opening comment's timezone-aware `createdAt`, or None when it carries
            none, carries one that does not parse, or carries a date with no timezone.
    """

    worktree: str
    opened_at: datetime | None

    def age_days(self, now: datetime | None = None) -> int | None:
        """Whole days the claim has been held at ``now`` (the clock when None), or None."""
        if self.opened_at is None:
            return None
        return ((now if now is not None else datetime.now(UTC)) - self.opened_at).days


@dataclass(frozen=True)
class Issue:
    """One register issue, parsed once from gh's JSON by `parse_issue` or `gh_calls`.

    Attributes:
        number: the issue number.
        title: the issue title.
        state: open or closed.
        body: the issue body.
        labels: every label name.
        comments: every comment in gh's order, at most `COMMENT_PAGE_CAP`.
        blockers: every issue recorded as blocking this one, open or closed.
        state_reason: gh's `stateReason`, such as `COMPLETED`, or "".
        created_at: gh's ISO-8601 `createdAt`, or "".
        closed_at: gh's ISO-8601 `closedAt`, or "" while it is open.
        url: the issue's web URL, or "".
        author: the login that opened it, or "" when gh returned none.
        issue_type: the name of its issue type, or None.
        milestone: gh's milestone object, which `milestone.branch_of` reads, or None.
        fields: its issue-field values by field name, which `gh_calls.attach_fields` fills.
        closing_prs: (`owner/name`, number) for each PR GitHub records as closing it.
    """

    number: int
    title: str
    state: IssueState
    body: str = ""
    labels: frozenset[str] = frozenset()
    comments: tuple[Comment, ...] = ()
    blockers: tuple[Blocker, ...] = ()
    state_reason: str = ""
    created_at: str = ""
    closed_at: str = ""
    url: str = ""
    author: str = ""
    issue_type: str | None = None
    milestone: Mapping[str, object] | None = field(default=None, hash=False)
    fields: Mapping[str, str] = field(default_factory=dict, hash=False)
    closing_prs: tuple[tuple[str, int], ...] = ()

    @property
    def is_open(self) -> bool:
        return self.state == IssueState.OPEN

    @property
    def milestone_title(self) -> object:
        return (self.milestone or {}).get("title")

    @property
    def severity(self) -> str | None:
        """This issue's severity as one of `SEVERITIES`, or None.

        Read from the Severity issue field, falling back to a `severity/<level>` label.
        """
        held = self.fields.get(SEVERITY_FIELD)
        if held:
            return held.lower()
        return _prefixed(self.labels, SEVERITY_PREFIX)

    @property
    def kind(self) -> str | None:
        """This issue's kind as one of `KINDS`, or None.

        Read from the issue type, falling back to a `kind/<kind>` label, where `addition`
        was the name for `feature`.
        """
        by_type = {name: k for k, name in KIND_TYPES.items()}
        if self.issue_type in by_type:
            return by_type[self.issue_type]
        label = _prefixed(self.labels, KIND_PREFIX)
        return "feature" if label == "addition" else label

    @property
    def not_before(self) -> date | None:
        """The date before which this issue is withheld from `next` and `claim`, or None.

        Read from the Not Before issue field, not a body trailer, and on purpose: this repo
        is public, so a trailer needs the author check the claim fold does, while setting an
        issue field takes triage access or more, as applying a label does. A date also
        expires on its own — the callers compare it to today and nobody has to clear it,
        which is what `manual` lacks.

        A `not-before:<YYYY-MM-DD>` label counts too, and the LATEST of the field and every
        such label wins, so a session on a pre-field checkout that defers by label still
        withholds the issue, and a stray earlier date cannot un-defer it. A value whose date
        does not parse counts as no deferral: `issue_rows` feeds the docs cron, which must
        not fall over on a hand-typed label.
        """
        held = self.fields.get(NOT_BEFORE_FIELD)
        texts = [held] if held else []
        texts += [
            name[len(NOT_BEFORE_PREFIX) :]
            for name in self.labels
            if name.startswith(NOT_BEFORE_PREFIX)
        ]
        days = []
        for text in texts:
            try:
                days.append(date.fromisoformat(text))
            except ValueError:
                continue
        return max(days) if days else None

    def deferred_on(self, today: date) -> bool:
        """Whether this issue is withheld on ``today``. `not-before: D` means workable ON D."""
        day = self.not_before
        return day is not None and today < day

    @property
    def open_blockers(self) -> list[str]:
        """The still-open issues blocking this one, as `#n` or `owner/repo#n`, sorted.

        Read from GitHub's native "blocked by" relationship, which `block` and `open
        --blocked-by` write. A closed blocker no longer withholds anything, so the issue
        becomes workable the moment its last blocker closes, with nothing to clear. A
        blocker in another repository is spelled with its slug.
        """
        found = []
        for b in self.blockers:
            if b.state != IssueState.OPEN:
                continue
            m = _ISSUE_URL_RE.match(b.url)
            repo = (
                "" if m is None or m.group(1).lower() == PR_REPO.lower() else m.group(1)
            )
            found.append((repo, b.number))
        return [f"{repo}#{n}" for repo, n in sorted(found)]

    @cached_property
    def claim(self) -> Claim | None:
        """The claim currently holding this issue, or None; see `claim_of`."""
        return claim_of(self.comments)

    @property
    def holder(self) -> str | None:
        """The worktree named by `claim`, or None when nothing holds this issue."""
        return self.claim.worktree if self.claim else None


def ordered_comments(comments: tuple[Comment, ...]) -> tuple[Comment, ...]:
    """``comments`` in `createdAt` order, or in gh's own order when it cannot say.

    THE ORDER IS THE PROTOCOL. `claim_of` folds forward and implements FIRST WRITER WINS,
    so its answer is only correct if the list really is oldest-first. gh returns them that
    way — measured ascending across 17 issues — but nothing enforced it.

    SORTS ONLY WHEN EVERY COMMENT CARRIES A TIMESTAMP. A mixed list is the dangerous case: a
    key of `created_at or ""` hoists every unstamped comment to the front, which can flip
    the verdict of the one function in this protocol that must not change by accident.
    Fixtures routinely omit `createdAt`, so the mixed list is not hypothetical.
    """
    if not all(c.created_at for c in comments):
        return comments
    return tuple(sorted(comments, key=lambda c: c.created_at))


def claim_of(comments: tuple[Comment, ...]) -> Claim | None:
    """The claim ``comments`` leave open, or None.

    Folds `ordered_comments` forward: a ``Claim:`` line opens IF nothing is currently held,
    and a ``Released:`` line naming the SAME worktree closes. The claim's `opened_at` is the
    `createdAt` of the comment that opened it.

    FIRST WRITER WINS, not last. gh returns comments in createdAt order, so the earlier of
    two racing claims is the earlier comment. Letting a later claim overwrite a live one
    would mean a session could take an issue out from under another by claiming it again,
    and — worse — the first claimant's own ``release`` would then be refused, so it could
    not even clean up after losing.

    A release naming some other worktree is ignored, so one session cannot release
    another's claim by accident.

    Only the OPERATOR's comments are folded — see `Comment.by_operator`. This repo is
    public, so a comment from any other account is prose on the thread and nothing more.
    """
    held: Claim | None = None
    for comment in ordered_comments(comments):
        if not comment.by_operator:
            continue
        claimed = _CLAIM_RE.search(comment.body)
        if claimed:
            if held is None:
                held = Claim(claimed.group(1), _aware(comment.created_at))
            # DECIDED: `Claim:` wins over `Released:` within ONE comment body. Neither
            # `claim_comment` nor `release_comment` ever emits both trailers, so a body
            # carrying both is malformed input, and every fail-safe in this module points
            # the same way: `Comment.by_operator` folds an unattributable comment into
            # nothing, `claim_is_live` HOLDS a claim whose worktree it cannot read,
            # `cmd_reap` refuses on a git error, and `cmd_next` withholds. Releasing on
            # ambiguity is the one direction that hands live work to a second session,
            # which is the harm this protocol exists to prevent. This `continue` is what
            # implements the choice, so it is not a tidy-up — dropping it inverts the
            # verdict.
            continue
        released = _RELEASE_RE.search(comment.body)
        if released and held is not None and released.group(1) == held.worktree:
            held = None
    return held


def _aware(stamp: str) -> datetime | None:
    """``stamp`` as a timezone-aware datetime, or None when it is empty, unparseable or naive."""
    try:
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo is not None else None


def parse_comment(data: Mapping) -> Comment:
    """One `gh issue ... --json comments` entry as a `Comment`."""
    return Comment(
        body=str(data.get("body") or ""),
        author=str((data.get("author") or {}).get("login") or ""),
        association=str(data.get("authorAssociation") or ""),
        viewer_did_author=bool(data.get("viewerDidAuthor")),
        created_at=str(data.get("createdAt") or ""),
    )


def parse_issue(data: Mapping) -> Issue:
    """One `gh issue list|view --json` object as an `Issue`.

    Raises:
        KeyError: a field every reader depends on is missing: `number`, `title`, `state`,
            `body`, `labels`, `comments` or `blockedBy`, or a blocker's `number` or `state`.
            gh returns an empty list or object for each when there is nothing to report, so
            an absent key means the fetch stopped asking for it. Read with a default, a
            missing `state` read as open and a missing blocker state as unblocked.
        ValueError: `state` is neither `OPEN` nor `CLOSED`.
    """
    closing = []
    for ref in data.get("closedByPullRequestsReferences") or []:
        where = ref.get("repository") or {}
        owner = (where.get("owner") or {}).get("login")
        closing.append((f"{owner}/{where.get('name')}", int(ref["number"])))
    return Issue(
        number=int(data["number"]),
        title=str(data["title"]),
        state=IssueState(data["state"]),
        body=str(data["body"] or ""),
        labels=frozenset(str(label["name"]) for label in data["labels"]),
        comments=tuple(parse_comment(c) for c in data["comments"]),
        blockers=tuple(
            Blocker(int(b["number"]), IssueState(b["state"]), str(b.get("url") or ""))
            for b in (data["blockedBy"] or {}).get("nodes") or []
        ),
        state_reason=str(data.get("stateReason") or ""),
        created_at=str(data.get("createdAt") or ""),
        closed_at=str(data.get("closedAt") or ""),
        url=str(data.get("url") or ""),
        author=str((data.get("author") or {}).get("login") or ""),
        issue_type=(data.get("issueType") or {}).get("name"),
        milestone=data.get("milestone"),
        closing_prs=tuple(closing),
    )


# `\s*$`, not `$`: API bodies can carry CRLF, which `re.M`'s `$` does not match before.
_FP_RE = re.compile(r"^Fingerprint: `([0-9a-f]{12})`\s*$", re.M)
_LINE_SUFFIX = re.compile(r":\d+(?:-\d+)?$")
_REOBSERVED = "Re-observed"
# The section runs from its own heading to the next one, or to the trailer's `---` rule, or
# to the end of the body. DOTALL so `.` crosses the paragraph's newlines.
#
# The heading must be `## Verify-by` ALONE on its line. `[ \t]*$` rather than `\s*` is
# load-bearing, and the whole reason a wider match is not used: some bodies carry a `##
# Verify-by, deliberately omitted` heading whose body explains why they have no
# instructions. A heading pattern that admitted a suffix would read those explanations back
# as the instructions themselves — the register would report three findings as verifiable
# when what their sections say is the opposite.
_VERIFY_BY_RE = re.compile(
    r"^## Verify-by[ \t]*\n(.*?)(?=^## |^---[ \t]*$|\Z)", re.M | re.S
)
_FENCED_RE = re.compile(r"\A```[^\n]*\n(.*?)\n?```[ \t]*\Z", re.S)


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


def verify_by_section(instructions: str) -> str:
    """The `## Verify-by` body section `open --verify-by` appends before the trailer.

    Writes the instructions as plain prose. They are not fenced: a fence says "this is a
    command to run", and nothing runs a verify-by any more.
    """
    return f"\n\n## Verify-by\n{instructions.strip()}\n"


def parse_verify_by(body: str) -> str | None:
    """The verification instructions stored in an issue body, or None.

    Reads the section `verify_by_section` writes, so a body a human has edited around still
    parses. A body fetched from the API can carry CRLF line endings, so they are normalized
    first.

    A fence around the whole section is stripped, which keeps older bodies readable: `open
    --verify-by` once stored a shell command in a code block.
    """
    m = _VERIFY_BY_RE.search((body or "").replace("\r\n", "\n"))
    if not m:
        return None
    section = m.group(1).strip()
    fenced = _FENCED_RE.match(section)
    if fenced:
        section = fenced.group(1).strip()
    return section or None


# Files only; the first segment is a bare directory so a hostname never matches.
_CITED_PATH_RE = re.compile(
    r"(?<![\w/.:-])(\.?[A-Za-z0-9_-]+(?:/[A-Za-z0-9_.-]+)+\.[A-Za-z0-9]+)(?::\d+)?(?![\w/])"
)


# Cut the trailer before scanning: it names `scripts/dev/findings.py` on every issue.
_TRAILER_RE = re.compile(r"^---[ \t]*\nFingerprint: `[0-9a-f]+`\n.*\Z", re.M | re.S)
_CODE_SPAN_RE = re.compile(r"`[^`\n]*`")
_BARE_NAME_RE = re.compile(r"([A-Za-z0-9_.-]*\.[A-Za-z0-9_-]+)(?::[\w.-]+)?")


def _drop_quoted_output(span: re.Match) -> str:
    """Blank an inline code span that lists two or more paths with commas, else keep it.

    Such a span quotes program output, such as a session banner line or a `next --json` list,
    and names files the issue does not touch. A real citation puts one path in its own span.
    """
    text = span.group(0)
    if "," in text and len(_CITED_PATH_RE.findall(text)) >= 2:
        return ""
    return text


def _named_by(spelled: str, tracked: Collection[str]) -> list[str]:
    """The tracked files whose path is `spelled` or ends with it at a directory boundary."""
    return [t for t in tracked if t == spelled or t.endswith(f"/{spelled}")]


def cited_paths(body: str, tracked: Collection[str] = ()) -> list[str]:
    """Every file an issue body cites, as a repo-root path where one is known, sorted.

    This is the one answer to "which files does this issue touch". The fan-out's two
    shared-file gates, the brief and the `paths` field of `next --json` all read it, so two
    issues that cite one script are one batch, whatever their Domain field says. The
    `--file` an issue was filed with lands only in the fingerprint, so the body's own
    citations are the record.

    The trailer is cut first: it is `findings.py`'s own text, not the finding's. So is an
    inline code span that lists several paths with commas, which quotes program output.

    A body spells a file three ways, and `tracked` decides the last two:

    - A path from the repo root (`scripts/dev/fanout_place.py`) is returned as written.
    - A path from a subdirectory (`fanout_lib/clean.py`) becomes the one tracked file that
      ends with it. With no such file, or several, it stays as written: it may name a file
      the fix creates.
    - A filename with no directory, alone in an inline code span (`` `ci.yml` ``), becomes
      the one tracked file of that name. With no such file, or several, it is dropped: a
      bare word with a dot in it is more often a version or an attribute than a file.

    Args:
        body: the issue body, as gh returns it.
        tracked: the tracked files of the repo the issue is about. Empty resolves nothing,
            so every path comes back as the body spells it and no bare filename counts.
    """
    text = _TRAILER_RE.sub("", (body or "").replace("\r\n", "\n"))
    text = _CODE_SPAN_RE.sub(_drop_quoted_output, text)
    paths = set()
    for spelled in {m.group(1) for m in _CITED_PATH_RE.finditer(text)}:
        hits = [spelled] if spelled in tracked else _named_by(spelled, tracked)
        paths.add(hits[0] if len(hits) == 1 else spelled)
    for span in _CODE_SPAN_RE.finditer(text):
        bare = _BARE_NAME_RE.fullmatch(span.group(0)[1:-1])
        hits = _named_by(bare.group(1), tracked) if bare else []
        if len(hits) == 1:
            paths.add(hits[0])
    return sorted(paths)


_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\n")
_WAIT_WORD_RE = re.compile(
    r"(?i)\b(?:once|after|until|waits? (?:on|for)|blocked (?:by|on)|depends on)\b"
)
_ISSUE_REF_RE = re.compile(r"(?<![\w/])#(\d+)\b")
# A target-architecture slice code as the plans spell one: `R4`, `M6`, `H7.53`, `H2.8c`.
_SLICE_RE = re.compile(r"\b([A-Z]\d+(?:\.\d+[a-z]?\d*)?)\b")


def waited_on(body: str) -> tuple[list[int], list[str]]:
    """The issue numbers and slice codes ``body`` says the finding waits on, first seen first.

    A reference counts only when it follows a wait word ("once", "after", "waits on") in the
    same sentence, so a body that merely cites `#n` as history yields nothing. The trailer
    is cut first. This is the evidence `open` and `defer` hint from: a finding that waits on
    other work wants `block`, not a guessed date.
    """
    text = _TRAILER_RE.sub("", (body or "").replace("\r\n", "\n"))
    numbers: list[int] = []
    codes: list[str] = []
    for sentence in _SENTENCE_RE.split(text):
        wait = _WAIT_WORD_RE.search(sentence)
        if not wait:
            continue
        tail = sentence[wait.start() :]
        for n in (int(m) for m in _ISSUE_REF_RE.findall(tail)):
            if n not in numbers:
                numbers.append(n)
        for code in _SLICE_RE.findall(tail):
            if code not in codes:
                codes.append(code)
    return numbers, codes


def today_utc() -> date:
    """The date `next`, `claim` and `list` compare a not-before label to; UTC like `now_iso`."""
    return datetime.now(UTC).date()


def find_by_fingerprint(issues: list[Issue], fp: str) -> Issue | None:
    for issue in issues:
        m = _FP_RE.search(issue.body)
        if m and m.group(1) == fp:
            return issue
    return None


def reobservations(issue: Issue) -> int:
    return sum(1 for c in issue.comments if c.body.startswith(_REOBSERVED))


# Re-observations that make a finding escalated: the filing plus two is its third sighting.
ESCALATED_AT = 2


NOT_PLANNED_PREFIX = {"refuted": "Refuted", "accepted": "Accepted"}
_SETTLED_PREFIXES = tuple(f"{p}: " for p in NOT_PLANNED_PREFIX.values())


def fold_marker(body: str) -> str | None:
    """Names the register marker ``body`` carries, or None for a comment the fold ignores.

    The register reads issue state back out of comments: a `Re-observed` prefix counts a
    sighting, a `Refuted: `/`Accepted: ` prefix records a close reason, and a `Claim:` or
    `Released:` trailer line moves a claim. A free-text comment carrying one would change
    that state without the verb that owns it.
    """
    text = body.strip()
    if text.startswith(_REOBSERVED):
        return f"a `{_REOBSERVED}` re-observation"
    if text.startswith(_SETTLED_PREFIXES):
        return f"a `{text.split(':', 1)[0]}:` close reason"
    if _CLAIM_RE.search(text):
        return "a `Claim:` trailer line"
    if _RELEASE_RE.search(text):
        return "a `Released:` trailer line"
    return None


def settled_reason(issue: Issue) -> str | None:
    """The `--reason` the operator's last `--accepted`/`--refuted` close recorded, first line."""
    for c in reversed(issue.comments):
        if not c.by_operator:
            continue
        body = c.body.strip()
        if body.startswith(_SETTLED_PREFIXES):
            return body.split(":", 1)[1].strip().splitlines()[0]
    return None


# Claims are append-only comments and the first writer wins: a later claim cannot overwrite a live
# one.
_CLAIM_RE = re.compile(r"^Claim: `([^`\n]+)`\s*$", re.M)
_RELEASE_RE = re.compile(r"^Released: `([^`\n]+)`\s*$", re.M)

# The repo is public: trust a claim trailer only from the operator (`viewerDidAuthor` or
# OWNER/MEMBER/COLLABORATOR).
_TRUSTED_ASSOCIATIONS = frozenset(("OWNER", "MEMBER", "COLLABORATOR"))


def _one_line(text: str) -> str:
    """``text`` with every line break collapsed to a space.

    THE PROSE ABOVE A TRAILER IS PARSED TOO. `Issue.claim` tests `_CLAIM_RE` before
    `_RELEASE_RE` on the WHOLE comment body, and `release_comment` puts the reason above the
    trailer — so a reason carrying a line ``Claim: `x` `` makes a release read as a claim by
    `x`. That is not a hypothetical hand-typed `--reason`: `cmd_reap` builds its own reason
    from `classify`'s text, which embeds a worktree's `lock_reason`, which is free text
    nobody in this repo writes.

    Collapsing the breaks closes it at the builder rather than at argparse, which is where
    the named vector arrives — `_CLAIM_RE` is anchored with `re.M`, so a trailer that cannot
    start a line cannot match at all. Anchoring the parser to the body's last trailer line
    was the alternative and is rejected: it inverts the DECIDED marker in `Issue.claim`
    that makes a `Claim:` line win over a `Released:` one, which points the other way on
    purpose.
    """
    return " ".join((text or "").splitlines())


def now_iso() -> str:
    """The `when` every claim and release comment is stamped with.

    Lives beside the two comment builders that consume it: `findings.py` and
    `findings_lib/claim_cli.py` both stamp comments, and a private `_now` in one of them would
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
    a release into a claim by whoever the reason names.
    """
    why = f" — {_one_line(reason)}" if reason else ""
    return f"Released by `{worktree}` at {when}{why}\n\nReleased: `{worktree}`\n"


# gh returns at most 100 comments; past that the claim fold misses trailers, so this warns.
COMMENT_PAGE_CAP = 100


def comment_cap_warning(number: int, n: int) -> str | None:
    """A warning when issue ``number`` carries ``n`` comments, as many as gh returns, else None.

    Pure, so `gh_calls` decides where it is printed and the rule itself is testable
    without a boundary.
    """
    if n < COMMENT_PAGE_CAP:
        return None
    return (
        f"warning: #{number} has {n} comments, gh's page cap — a claim or "
        "release past the cap is invisible to the fold, so its claim verdict may be wrong"
    )


PR_REPO = "DanielH2018/server"
_ISSUE_URL_RE = re.compile(r"https?://github\.com/([\w.-]+/[\w.-]+)/issues/\d+")

# GitHub closes on close/fix/resolve forms; `pr_refs` uses the captured owner/repo to ignore other
# repositories' numbers.
_PR_REF_RE = re.compile(
    r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+"
    r"(?:https?://github\.com/([\w.-]+/[\w.-]+)/issues/|(?:([\w.-]+/[\w.-]+))?#)(\d+)\b"
)


def pr_refs(bodies: list[str], repo: str = PR_REPO) -> set[int]:
    """Issue numbers the given PR bodies say they close, in `repo`.

    A bare `#n` counts, and so does `owner/repo#n` or `https://github.com/owner/repo/issues/n`
    when the qualifier names `repo` -- compared case-insensitively, the way GitHub resolves a
    slug. The full `owner/repo` is compared, not the repository name alone, so a fork's
    `SomeoneElse/server#n` does not withhold anything, and neither does its URL.
    """
    return {
        int(number)
        for body in bodies
        for url_repo, slug_repo, number in _PR_REF_RE.findall(body or "")
        if not (url_repo or slug_repo)
        or (url_repo or slug_repo).lower() == repo.lower()
    }


def _prefixed(names: frozenset[str], prefix: str) -> str | None:
    """The first label under ``prefix``, alphabetically.

    Iterating the set directly would pick an arbitrary one of two `severity/*` labels, so
    the same issue could render two different rows on two runs.
    """
    for n in sorted(names):
        if n.startswith(prefix):
            return n[len(prefix) :]
    return None


_STATE_REASONS = {
    "COMPLETED": "fixed",
    "NOT_PLANNED": "not planned",
    "DUPLICATE": "duplicate",
}


def close_outcome(issue: Issue) -> str:
    """How ``issue`` was closed: `open`, `refuted`, `accepted`, `fixed`, `not planned`, ...

    The label decides before `stateReason` does, because `findings.py close` writes the
    label and gh records both not-planned outcomes as the same `NOT_PLANNED`. `stateReason`
    is read only on a closed issue: a reopened one carries `REOPENED`.
    """
    if issue.is_open:
        return "open"
    for settled in ("refuted", "accepted"):
        if settled in issue.labels:
            return settled
    reason = issue.state_reason
    return _STATE_REASONS.get(reason, reason.lower() or "closed")


def issue_rows(issues: list[Issue], tracked: Collection[str] = ()) -> list[dict]:
    """Flattens issues into the rows ``sort_key`` and ``cmd_list`` consume.

    Args:
        issues: the issues to flatten.
        tracked: the register's tracked files, which each row's ``paths`` resolve against
            as `cited_paths` describes.
    """
    rows = []
    for issue in issues:
        names = issue.labels
        day = issue.not_before
        seen = reobservations(issue)
        rows.append(
            {
                "number": issue.number,
                "title": issue.title,
                "state": issue.state.value,
                "severity": issue.severity,
                "kind": issue.kind,
                "domain": issue.fields.get(DOMAIN_FIELD),
                "escalated": seen >= ESCALATED_AT,
                "refuted": "refuted" in names,
                "accepted": "accepted" in names,
                "reason": settled_reason(issue),
                "outcome": close_outcome(issue),
                "no_vetted_remediation": "no-vetted-remediation" in names,
                "verify_by": parse_verify_by(issue.body) is not None,
                "paths": cited_paths(issue.body, tracked),
                "manual": "manual" in names,
                "review_leftover": "review-leftover" in names,
                "not_before": day.isoformat() if day else None,
                "blocked_by": issue.open_blockers,
                "claimed": issue.holder,
                "first_seen": issue.created_at[:10],
                "closed_on": issue.closed_at[:10] or None,
                "reobservations": seen,
                "url": issue.url,
            }
        )
    return rows


def pickable(
    issues: list[Issue],
    *,
    live_claims: set[int],
    pr_refs: set[int],
    today: date,
    tracked: Collection[str] = (),
) -> list[dict]:
    """The issues a session may pick up, best first.

    Args:
        live_claims: issue numbers whose claim is still live. A STALE claim does not
            withhold an issue — `cmd_claim` reaps one on its way past, and `reap` clears
            every one of them at once.
        pr_refs: issue numbers an open PR already says it closes. Without this, a session
            picks up work another session has finished but not yet landed.
        today: the date a Not Before date is compared to. Passed in, like `when` is to
            `plan_claim`, so the boundary is testable: an issue is offered ON its date.
        tracked: the register's tracked files, passed on to `issue_rows`.
    """
    rows = [
        r
        for r in issue_rows(issues, tracked)
        if not r["manual"]
        and not is_waiting_row(r, today)
        and r["number"] not in live_claims
        and r["number"] not in pr_refs
    ]
    return sorted(rows, key=lambda r: (r["review_leftover"], sort_key(r)))


def waiting(
    issues: list[Issue], *, today: date, tracked: Collection[str] = ()
) -> list[dict]:
    """The rows `pickable` withholds for a not-before date or an open blocker, best first.

    `next` prints these beside the free set: a silently withheld issue reads as a closed one
    to the operator looking at the backlog, which is the failure `manual` has.
    """
    rows = [
        r
        for r in issue_rows(issues, tracked)
        if not r["manual"] and is_waiting_row(r, today)
    ]
    return sorted(rows, key=sort_key)


def is_deferred_row(row: dict, today: date) -> bool:
    """Whether ``row``'s not-before date still withholds it on ``today``."""
    return row["not_before"] is not None and today < date.fromisoformat(
        row["not_before"]
    )


def is_waiting_row(row: dict, today: date) -> bool:
    """Whether a date or an open blocker withholds ``row``; either one alone suffices."""
    return bool(row["blocked_by"]) or is_deferred_row(row, today)


def waiting_line(row: dict, today: date) -> str:
    """The line `next` prints for a waiting ``row``: what withholds it, then its title."""
    n = row["number"]
    if row["blocked_by"]:
        line = f"blocked: #{n} by {', '.join(row['blocked_by'])}"
        if is_deferred_row(row, today):
            line += f" and until {row['not_before']}"
    else:
        line = f"deferred: #{n} until {row['not_before']}"
    return f"{line}  {row['title']}"


def sort_key(row: dict) -> tuple:
    sev = (
        SEVERITIES.index(row["severity"])
        if row["severity"] in SEVERITIES
        else len(SEVERITIES)
    )
    return (sev, 0 if row["escalated"] else 1, row["number"])
