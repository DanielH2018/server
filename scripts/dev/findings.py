#!/usr/bin/env python3
"""File, re-observe, escalate and close Claude's unfixed findings as GitHub Issues.

WHY A WRAPPER. The homelab-review skill, the review-and-fix command and an ordinary session
all produce findings nobody fixes that day. Until 2026-09-02 they landed in a memory table
with no status field, a gitignored triage file that suppressed them, and session notes.
GitHub Issues has the status field; this script owns the three rules that make issues a
register rather than a pile: one issue per fingerprint, a re-observation is a comment and
the third one escalates, and a refuted finding stays closed.

Every command PLANS a list of gh argv first (pure, unit-tested), then runs it. `--dry-run`
prints the plan and writes nothing.

Usage::

    uv run python scripts/dev/findings.py sync-labels
    uv run python scripts/dev/findings.py open --title "..." --body-file f.md \\
        --severity high --kind gap [--domain network] [--file path/to/file.py:12] \\
        [--source review-2026-09-02] [--no-vetted-remediation] [--dry-run]
    uv run python scripts/dev/findings.py touch 688 [--source review-2026-09-02]
    uv run python scripts/dev/findings.py close 688 --fixed [--pr 700]
    uv run python scripts/dev/findings.py close 688 --refuted --reason "..."
    uv run python scripts/dev/findings.py list [--state open|closed|all] [--json]

Exit codes: 0 done; 1 gh failed (its stderr is printed); 2 bad arguments;
3 the fingerprint belongs to an issue closed as refuted, nothing was written.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from lib.gh import gh, gh_json  # noqa: E402

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
# call, so no second write and no item id. It needs the `project` token scope.
PROJECT_TITLE = "Claude findings"

_LIST_FIELDS = "number,title,state,labels,body,createdAt,closedAt,url,comments"
_FP_RE = re.compile(r"^Fingerprint: `([0-9a-f]{12})`$", re.M)
_LINE_SUFFIX = re.compile(r":\d+(?:-\d+)?$")
_REOBSERVED = "Re-observed"


# --- pure helpers ---------------------------------------------------------------------------


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
    for n in names:
        if n.startswith(prefix):
            return n[len(prefix) :]
    return None


def issue_rows(issues: list[dict]) -> list[dict]:
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


def plan_sync_labels(existing: set[str]) -> list[list[str]]:
    plans = []
    for name, (colour, desc) in LABELS.items():
        if name not in existing:
            plans.append(
                ["label", "create", name, "--color", colour, "--description", desc]
            )
    return plans


# --- gh-facing ------------------------------------------------------------------------------


def load_issues(state: str = "all") -> list[dict]:
    return (
        gh_json(
            "issue",
            "list",
            "--label",
            "claude",
            "--state",
            state,
            "--limit",
            "1000",
            "--json",
            _LIST_FIELDS,
        )
        or []
    )


def run(plans: list[list[str]], dry_run: bool) -> None:
    for argv in plans:
        if dry_run:
            print("gh " + " ".join(argv))
        else:
            gh(*argv)


# --- commands ---------------------------------------------------------------------------------


def plan_touch(issue: dict, source: str) -> list[list[str]]:
    """A re-observation is a comment; the third one adds `escalated`."""
    n = str(issue["number"])
    seen = reobservations(issue) + 1
    plans = [
        [
            "issue",
            "comment",
            n,
            "--body",
            f"{_REOBSERVED} by {source} (sighting {seen + 1}).",
        ]
    ]
    if seen >= 2 and "escalated" not in label_names(issue):
        plans.append(["issue", "edit", n, "--add-label", "escalated"])
    return plans


def plan_open(
    existing: dict | None,
    *,
    title: str,
    body: str,
    labels: list[str],
    fp: str,
    source: str,
) -> tuple[str, int, list[list[str]]]:
    if existing is None:
        argv = [
            "issue",
            "create",
            "--title",
            title,
            "--body",
            body + trailer(fp, source),
        ]
        for lab in labels:
            argv += ["--label", lab]
        argv += ["--project", PROJECT_TITLE]
        return "created", 0, [argv]
    n = str(existing["number"])
    names = label_names(existing)
    if existing.get("state") == "CLOSED" and "refuted" in names:
        return "refuted", 3, []
    if existing.get("state") == "CLOSED":
        return (
            "reopened",
            0,
            [
                ["issue", "reopen", n],
                [
                    "issue",
                    "comment",
                    n,
                    "--body",
                    f"{_REOBSERVED} by {source} after it was closed as fixed: treat as a regression.",
                ],
            ],
        )
    return "touched", 0, plan_touch(existing, source)


def cmd_open(args: argparse.Namespace) -> int:
    body = args.body_file.read_text()
    fp = fingerprint(args.title, args.file)
    labels = ["claude", f"severity/{args.severity}", f"kind/{args.kind}"]
    if args.domain:
        labels.append(f"domain/{args.domain}")
    if args.no_vetted_remediation:
        labels.append("no-vetted-remediation")
    existing = find_by_fingerprint(load_issues("all"), fp)
    outcome, code, plans = plan_open(
        existing, title=args.title, body=body, labels=labels, fp=fp, source=args.source
    )
    if outcome == "refuted":
        print(
            f"#{existing['number']} refuted: closed on {existing.get('closedAt', '?')[:10]}; not reopened"
        )
        return code
    if outcome == "created":
        if args.dry_run:
            run(plans, True)
            print(f"(dry-run) would create; fingerprint {fp}")
            return 0
        url = gh(*plans[0]).stdout.strip()
        print(f"#{url.rsplit('/', 1)[-1]} created  {url}")
        return 0
    run(plans, args.dry_run)
    print(f"#{existing['number']} {outcome}  {existing.get('url', '')}")
    return 0


def plan_close(
    number: int, *, fixed: bool, pr: int | None, reason: str | None
) -> list[list[str]]:
    n = str(number)
    if fixed:
        by = f" by PR #{pr}" if pr else ""
        return [
            ["issue", "close", n, "--reason", "completed", "--comment", f"Fixed{by}."]
        ]
    return [
        ["issue", "edit", n, "--add-label", "refuted"],
        [
            "issue",
            "close",
            n,
            "--reason",
            "not planned",
            "--comment",
            f"Refuted: {reason}",
        ],
    ]


def _load_issue(number: int) -> dict:
    return gh_json("issue", "view", str(number), "--json", _LIST_FIELDS)


def cmd_touch(args: argparse.Namespace) -> int:
    issue = _load_issue(args.number)
    plans = plan_touch(issue, args.source)
    run(plans, args.dry_run)
    escalated = any(p[:2] == ["issue", "edit"] for p in plans)
    print(f"#{args.number} touched{' and escalated' if escalated else ''}")
    return 0


def cmd_close(args: argparse.Namespace) -> int:
    if args.refuted and not args.reason:
        sys.stderr.write(
            "close --refuted needs --reason: a bare refutation teaches the next run nothing\n"
        )
        return 2
    run(
        plan_close(args.number, fixed=args.fixed, pr=args.pr, reason=args.reason),
        args.dry_run,
    )
    print(f"#{args.number} closed as {'fixed' if args.fixed else 'refuted'}")
    return 0


def cmd_sync_labels(args: argparse.Namespace) -> int:
    existing = {
        lab["name"]
        for lab in gh_json("label", "list", "--limit", "200", "--json", "name") or []
    }
    plans = plan_sync_labels(existing)
    run(plans, args.dry_run)
    print(f"sync-labels: {len(plans)} label(s) created")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    rows = sorted(issue_rows(load_issues(args.state)), key=sort_key)
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    for r in rows:
        flags = "".join(
            f" [{f}]"
            for f, on in (
                ("escalated", r["escalated"]),
                ("no-vetted-remediation", r["no_vetted_remediation"]),
            )
            if on
        )
        print(
            f"#{r['number']:<5} {r['severity'] or '-':<6} {r['kind'] or '-':<11} "
            f"{r['domain'] or '-':<21} since {r['first_seen']} x{r['reobservations']}{flags}  {r['title']}"
        )
    return 0


def _add_dry_run(parser: argparse.ArgumentParser, *, suppress: bool) -> None:
    """Add ``--dry-run`` to ``parser``.

    Every subparser gets its own copy so the flag parses on either side of the
    subcommand name — argparse only accepts a parent-parser optional before the
    subcommand token. ``suppress=True`` (used on the subparsers) sets
    ``default=argparse.SUPPRESS`` so an absent subparser flag leaves the top-level
    parser's own default in place instead of overwriting it back to ``False``.
    """
    default = argparse.SUPPRESS if suppress else False
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=default,
        help="print the gh commands, write nothing",
    )


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    _add_dry_run(p, suppress=False)
    sub = p.add_subparsers(dest="cmd", required=True)

    o = sub.add_parser(
        "open", help="file a finding, or touch/reopen the existing issue"
    )
    _add_dry_run(o, suppress=True)
    o.add_argument("--title", required=True)
    o.add_argument("--body-file", required=True, type=Path)
    o.add_argument("--severity", required=True, choices=SEVERITIES)
    o.add_argument("--kind", required=True, choices=KINDS)
    o.add_argument("--domain", choices=DOMAINS)
    o.add_argument(
        "--file",
        help="primary file:line the finding cites; the line is dropped from the fingerprint",
    )
    o.add_argument("--source", default="session", help="review-<date> or session")
    o.add_argument("--no-vetted-remediation", action="store_true")

    t = sub.add_parser(
        "touch", help="record a re-observation; the third adds escalated"
    )
    _add_dry_run(t, suppress=True)
    t.add_argument("number", type=int)
    t.add_argument("--source", default="session")

    c = sub.add_parser("close", help="close as fixed or refuted")
    _add_dry_run(c, suppress=True)
    c.add_argument("number", type=int)
    how = c.add_mutually_exclusive_group(required=True)
    how.add_argument("--fixed", action="store_true")
    how.add_argument("--refuted", action="store_true")
    c.add_argument("--pr", type=int, help="the PR that fixed it")
    c.add_argument("--reason", help="required with --refuted: what disproved it")

    ls = sub.add_parser("list", help="rows for the review skill and the docs generator")
    _add_dry_run(ls, suppress=True)
    ls.add_argument("--state", default="open", choices=("open", "closed", "all"))
    ls.add_argument("--json", action="store_true")

    sl = sub.add_parser("sync-labels", help="create any missing label")
    _add_dry_run(sl, suppress=True)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    handler = {
        "sync-labels": cmd_sync_labels,
        "list": cmd_list,
        "open": cmd_open,
        "touch": cmd_touch,
        "close": cmd_close,
    }[args.cmd]
    try:
        return handler(args)
    except subprocess.CalledProcessError as exc:
        sys.stderr.write(f"gh failed ({exc.returncode}): {exc.stderr.strip()}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
