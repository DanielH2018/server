"""The two history subcommands: `history` searches past findings, `show` prints one.

Split out of `findings.py` to keep that file under its 600-line cap, the same reason
`findings_lib/claim_cli.py` exists. These two are the only handlers that read CLOSED issues on
purpose, and they read them through gh's search index rather than the register list.

WHY SEARCH AND NOT `list --state closed`. A label-filtered `gh issue list` goes through
GitHub's search API, which stops at 1000 issues whatever `--limit` says, and asking for `body`
and `comments` on every one took 58.4s for 995 issues. The register passed 1000 closed
issues in 2026-10, so the oldest findings were unreachable and the read outran a 120s tool
timeout. A search for a topic returns tens of issues in under a second and does not grow with
the register. `list` reads the open register only.

WHAT A SESSION NEEDS FROM HISTORY. Whether a finding was settled before, and how: the close
outcome (fixed, refuted, accepted, or a hand close), the date, the `--reason` a not-planned
close recorded, and the PR that fixed it. `issue_rows` carries none of the first, the second
or the last, so these rows are built here from two extra gh fields.
"""

import argparse
import json
import re
import sys

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

# DECIDED: imported as `dev.findings_lib.<leaf>`, never as bare siblings — see the marker in findings.py.
from dev.findings_lib.boundaries import FindingsTools
from dev.findings_lib.gh_calls import HISTORY_FIELDS, search_register
from dev.findings_lib.issue_model import (
    _LINE_SUFFIX,
    PR_REPO,
    _TRAILER_RE,
    cited_paths,
    is_operator_comment,
    label_names,
    settled_reason,
)
from dev.findings_lib.precomputed import precomputed
from lib.json_types import as_object

# `plan_close` writes this comment on `close --fixed --pr N`. A close that way leaves
# `closedByPullRequestsReferences` empty, because no closing keyword in a PR closed the issue.
_FIXED_BY_RE = re.compile(r"^Fixed by PR #(\d+)\.", re.M)

# gh's `stateReason` for a close with no `refuted`/`accepted` label. A `NOT_PLANNED` close
# without a label is a hand close, so it reads as that rather than as refuted.
_STATE_REASONS = {
    "COMPLETED": "fixed",
    "NOT_PLANNED": "not planned",
    "DUPLICATE": "duplicate",
}


def close_outcome(issue: dict) -> str:
    """How ``issue`` was closed: `open`, `refuted`, `accepted`, `fixed`, `not planned`, ...

    The label decides before `stateReason` does, because `findings.py close` writes the
    label and gh records both not-planned outcomes as the same `NOT_PLANNED`. `stateReason`
    is read only on a closed issue: a reopened one carries `REOPENED`.
    """
    if issue.get("state") != "CLOSED":
        return "open"
    names = label_names(issue)
    for settled in ("refuted", "accepted"):
        if settled in names:
            return settled
    reason = issue.get("stateReason") or ""
    return _STATE_REASONS.get(reason, reason.lower() or "closed")


def closing_prs(issue: dict, repo: str) -> list[int]:
    """The PR numbers that closed ``issue``, sorted, from either record of it.

    Args:
        issue: a gh issue carrying `closedByPullRequestsReferences` and `comments`.
        repo: the register's `OWNER/NAME`. A reference to a PR in another repo is dropped,
            because a bare `#N` printed beside it would name this repo's PR N.
    """
    prs = set()
    for ref in issue.get("closedByPullRequestsReferences") or []:
        where = ref.get("repository") or {}
        slug = f"{(where.get('owner') or {}).get('login')}/{where.get('name')}"
        if slug.lower() == repo.lower():
            prs.add(ref["number"])
    for comment in issue.get("comments", []):
        # This repo is public; a `Fixed by PR` line from any other account records nothing.
        if is_operator_comment(comment):
            prs.update(int(n) for n in _FIXED_BY_RE.findall(comment.get("body") or ""))
    return sorted(prs)


def history_row(issue: dict, repo: str) -> dict:
    """One `history` row: what a session needs to judge whether a finding was settled."""
    return {
        "number": issue["number"],
        "title": issue["title"],
        "outcome": close_outcome(issue),
        "closed": (issue.get("closedAt") or "")[:10] or None,
        "reason": settled_reason(issue),
        "prs": closing_prs(issue, repo),
        "url": issue.get("url", ""),
    }


def cites(issue: dict, path: str) -> bool:
    """Whether ``issue``'s body cites ``path``, or a file under it when it is a directory.

    Not `cited_paths`: it needs a `/` and a file extension, so it never captures `CLAUDE.md`,
    `prek.toml` or `bin/land`, and `--file CLAUDE.md` dropped all 30 of gh's hits. The path
    matches as a whole token instead. Nothing path-like may precede it, so `CLAUDE.md` does
    not match `ansible/roles/k8s/docs/CLAUDE.md`; it may be followed by `/` (a directory),
    `:<line>` or sentence punctuation, but not by more of a name. The trailer is cut first,
    because it names `scripts/dev/findings.py` on every issue.
    """
    body = _TRAILER_RE.sub("", (issue.get("body") or "").replace("\r\n", "\n"))
    token = re.compile(rf"(?<![\w/.-]){re.escape(path.rstrip('/'))}(?![\w-]|\.\w)")
    return token.search(body) is not None


def _print_row(row: dict) -> None:
    print(
        f"#{row['number']:<5} {row['outcome']:<11} {row['closed'] or '-':<10}  "
        f"{row['title']}"
    )
    if row["reason"]:
        print(f"       reason: {row['reason']}")
    if row["prs"]:
        print("       closed by " + ", ".join(f"#{n}" for n in row["prs"]))


def cmd_history(args: argparse.Namespace, tools: FindingsTools) -> int:
    """Handles the ``history`` subcommand: findings in any state that match a topic or a file.

    Search narrows and the body decides for ``--file``. GitHub's tokenizer splits a path
    on `/` and `.`, so a phrase search for one path also returns issues citing its
    neighbours; only an issue whose body cites the path (`cites`) is printed.

    Args:
        args: parsed CLI namespace carrying ``terms``, ``file``, ``limit`` and ``json``.
        tools: the process boundaries the search goes through.

    Returns:
        2 if neither search terms nor ``--file`` were given, 0 otherwise.
    """
    if not args.terms and not args.file:
        sys.stderr.write("history: need search terms, --file, or both\n")
        return 2
    path = _LINE_SUFFIX.sub("", args.file.strip()) if args.file else None
    query = " ".join([*args.terms, *([f'"{path}"'] if path else [])])
    hits = search_register(query, tools, limit=args.limit)
    if len(hits) >= args.limit:
        # Counted before the `--file` filter: that is the page gh truncated.
        sys.stderr.write(
            f"warning: {args.limit} search hits for {query!r}, the --limit -- older "
            "matches may be past it; narrow the terms or raise --limit\n"
        )
    if path:
        hits = [h for h in hits if cites(h, path)]
    rows = [history_row(h, tools.repo or PR_REPO) for h in hits]
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    for row in rows:
        _print_row(row)
    if not rows:
        print("no findings match")
    return 0


def cmd_show(args: argparse.Namespace, tools: FindingsTools) -> int:
    """Handles the ``show`` subcommand: one finding's body, outcome, closing PR and thread.

    The thread is printed whole, so a refute or accept reason longer than the one line
    `history` shows is all there. Each comment names its author, because this repo is
    public and a comment from outside it is not the operator's word.

    Args:
        args: parsed CLI namespace carrying ``number`` and ``json``.
        tools: the process boundaries the issue read goes through.

    Returns:
        3 if the issue is not in the `claude` register, 0 otherwise.
    """
    # Read as a plain dict, the shape every `issue_model` reader takes.
    issue: dict = as_object(
        tools.gh_json("issue", "view", str(args.number), "--json", HISTORY_FIELDS),
        "gh issue view",
    )
    if "claude" not in label_names(issue):
        sys.stderr.write(f"show: #{args.number} is not a `claude` finding\n")
        return 3
    if args.brief:
        # The fan-out brief's block, for a session working the issue by hand (#3955).
        block = precomputed(cited_paths(issue.get("body") or ""))
        print(block.rstrip() if block else f"#{args.number} cites no repo path")
        return 0
    row = history_row(issue, tools.repo or PR_REPO)
    comments = [
        {
            "author": (c.get("author") or {}).get("login"),
            "operator": is_operator_comment(c),
            "createdAt": c.get("createdAt"),
            "body": c.get("body") or "",
        }
        for c in issue.get("comments", [])
    ]
    if args.json:
        print(
            json.dumps(
                {**row, "body": issue.get("body") or "", "comments": comments}, indent=2
            )
        )
        return 0
    _print_row(row)
    print(f"       {row['url']}\n")
    print((issue.get("body") or "").strip())
    for c in issue.get("comments", []):
        who = (c.get("author") or {}).get("login")
        if not is_operator_comment(c):
            who = f"{who} (not the operator)"
        day = (c.get("createdAt") or "")[:10]
        print(f"\n--- {day} {who}\n{(c.get('body') or '').strip()}")
    return 0
