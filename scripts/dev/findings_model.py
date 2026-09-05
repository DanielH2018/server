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
    "escalated": ("f5c2e7", "Re-observed three or more times; needs a durable owner"),
    "no-vetted-remediation": ("585b70", "Every proposed fix failed the fix-skeptic"),
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
                "no_vetted_remediation": "no-vetted-remediation" in names,
                "verify_by": parse_verify_by(issue.get("body") or "") is not None,
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
