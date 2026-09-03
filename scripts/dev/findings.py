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

VERIFY-BY. `open --verify-by '<command>'` stores a read-only shell command in the issue body
under a `## Verify-by` heading, in a fenced code block so it survives a human editing the
prose around it. `verify` re-runs that command later: exit 0 means the finding is FIXED,
non-zero means it still reproduces. It refuses to run anything the repo's own read-only
classifier (`.claude/hooks/auto-approve-readonly.py`, the same judgment that decides what a
session can run without a prompt) does not clear — a command stored by `open` but never
validated there is still only ever run through that gate.

Usage::

    uv run python scripts/dev/findings.py sync-labels
    uv run python scripts/dev/findings.py open --title "..." --body-file f.md \\
        --severity high --kind gap [--domain network] [--file path/to/file.py:12] \\
        [--source review-2026-09-02] [--no-vetted-remediation] \\
        [--verify-by 'uv run python scripts/diagnostics/probe.py health <svc>'] [--dry-run]
    uv run python scripts/dev/findings.py touch 688 [--source review-2026-09-02]
    uv run python scripts/dev/findings.py close 688 --fixed [--pr 700]
    uv run python scripts/dev/findings.py close 688 --refuted --reason "..."
    uv run python scripts/dev/findings.py list [--state open|closed|all] [--json]
    uv run python scripts/dev/findings.py verify --all [--close] [--timeout 120]
    uv run python scripts/dev/findings.py verify 688 701 [--close]

Exit codes: 0 done; 1 gh failed (its stderr is printed); 2 bad arguments;
3 nothing was written because the issue refuses it — the fingerprint belongs to an issue
closed as refuted, or `touch` was given a closed issue.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
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

from lib.gh import gh, gh_json
from lib.repo_paths import REPO

_READONLY_HOOK = REPO / ".claude" / "hooks" / "auto-approve-readonly.py"
# A narrow allowlist layered ON TOP of the hook's classify(), not just a fallback for when
# it fails to load. `uv` is opaque to classify() by design — `uv run <anything>` can exec
# anything, so TIER1/HANDLERS has no `uv` entry at all — which means the hook alone refuses
# every command this feature exists to run: the review skill's own examples are
# `uv run python scripts/diagnostics/probe.py ...` and `uv run pytest ...`. This regex covers
# EXACTLY those two shapes and no other script: `scripts/[\w./-]+\.py` would also admit
# `scripts/backup/b2_drain.py --yes` and `scripts/secrets_mgmt/secret_rotation.py rotate`,
# both of which mutate state, so the script path is pinned to `probe.py` by name rather than
# left open to anything under `scripts/`. Each argument is further restricted to a safe
# character class so an issue body a human edited to smuggle `; curl attacker.example`
# cannot slip through — `;`, `|`, `$`, backticks and quotes are all outside `_SAFE_ARG`.
_SAFE_ARG = r"[\w./=:,-]+"
_FALLBACK_VERIFY_RE = re.compile(
    rf"^uv run (python scripts/diagnostics/probe\.py(?:\s+{_SAFE_ARG})*"
    rf"|pytest(?:\s+{_SAFE_ARG})*)$"
)


def _load_readonly_classify():
    """The auto-approve-readonly hook's `classify()`, loaded by path, or None.

    The filename is hyphenated, so it is not importable by name. `block-protected-bash.py`
    loads its sibling hook the same way, for the same reason: one classifier judges both
    what a session can run without a prompt and what `findings.py verify` may execute.
    """
    if not _READONLY_HOOK.is_file():
        return None
    try:
        # The hook's own top-level `from _hook_common import ...` only resolves once its
        # directory is on sys.path — true automatically when it runs as the hook entry
        # point, not when loaded by path from here.
        hooks_dir = str(_READONLY_HOOK.parent)
        if hooks_dir not in _sys.path:
            _sys.path.insert(0, hooks_dir)
        spec = importlib.util.spec_from_file_location(
            "auto_approve_readonly", _READONLY_HOOK
        )
        if not spec or not spec.loader:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.classify
    except Exception:
        return None


def classify_verify_command(command: str) -> str | None:
    """A reason string if `command` is read-only by the repo's own standard, else None.

    A union, not an either-or: the hook's `classify()` clears it, OR the narrow `uv run`
    allowlist does (see `_FALLBACK_VERIFY_RE` for why that allowlist runs even when the hook
    loaded fine). Falls back to the allowlist alone when the hook cannot be loaded at all —
    this script is run outside the checkout that carries `.claude/`.
    """
    classify = _load_readonly_classify()
    reason = classify(command) if classify is not None else None
    if reason:
        return reason
    return (
        "fallback: uv run" if _FALLBACK_VERIFY_RE.fullmatch(command.strip()) else None
    )


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

_LIST_FIELDS = "number,title,state,labels,body,createdAt,closedAt,url,comments"
# `\s*$` rather than `$`: a body fetched from the API can carry CRLF line endings, and
# `re.M`'s `$` matches before the `\n` but not before the `\r`.
_FP_RE = re.compile(r"^Fingerprint: `([0-9a-f]{12})`\s*$", re.M)
_LINE_SUFFIX = re.compile(r":\d+(?:-\d+)?$")
_REOBSERVED = "Re-observed"
# DOTALL so `.` crosses the command's own newlines; the heading anchors it against prose
# a human added elsewhere in the body, and the fence is stripped by the capture group.
_VERIFY_BY_RE = re.compile(r"^## Verify-by\s*\n```[^\n]*\n(.*?)\n```", re.M | re.S)


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


def without_project(argv: list[str]) -> list[str]:
    """``argv`` with the ``--project <title>`` pair removed.

    Removal is by position, not by value: a finding titled "Claude findings" would otherwise
    lose its own ``--title`` argument.
    """
    out = []
    skip = False
    for i, arg in enumerate(argv):
        if skip:
            skip = False
            continue
        if arg == "--project" and i + 1 < len(argv):
            skip = True
            continue
        out.append(arg)
    return out


def is_project_failure(stderr: str | None) -> bool:
    """Whether ``gh``'s stderr blames the Project rather than the issue.

    A missing board reads "could not resolve to a ProjectV2"; a token without the `project`
    scope reads "missing required scopes". Both mean the issue itself is fine.
    """
    low = (stderr or "").lower()
    return "project" in low or "scope" in low


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
    """Fetches every ``claude``-labeled issue from gh, up to 1000.

    Args:
        state: issue state to filter by (``open``, ``closed`` or ``all``).
    """
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


def _existing_labels() -> set[str]:
    return {
        lab["name"]
        for lab in gh_json("label", "list", "--limit", "200", "--json", "name") or []
    }


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
    verify_by: str | None = None,
) -> tuple[str, int, list[list[str]]]:
    """Plans the gh argv to file, touch or reopen a finding, given its matching issue.

    Args:
        existing: the issue matching this finding's fingerprint, or None if it is new.
        title: issue title.
        body: issue body, before the verify-by section and the fingerprint/source trailer
            are appended.
        labels: labels to apply on create.
        fp: the finding's fingerprint.
        source: the review or session that produced this finding.
        verify_by: a read-only command whose exit code later tells `verify` whether the
            finding is fixed; stored only when creating a new issue.

    Returns:
        A ``(outcome, exit_code, plans)`` tuple: outcome is one of ``created``, ``touched``,
        ``reopened`` or ``refuted``; exit_code is 3 only for ``refuted``; plans is the gh
        argv list to run.
    """
    if existing is None:
        full_body = body
        if verify_by:
            full_body += verify_by_section(verify_by)
        full_body += trailer(fp, source)
        argv = [
            "issue",
            "create",
            "--title",
            title,
            "--body",
            full_body,
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


def _create_with_optional_project(argv: list[str]) -> str:
    """Run the create argv, retrying without ``--project`` if the board is the only problem.

    Returns the created issue's URL. The board is a view; losing it must not lose the
    finding, so a Project failure warns and the issue is created anyway.
    """
    try:
        return gh(*argv).stdout.strip()
    except subprocess.CalledProcessError as exc:
        if not is_project_failure(exc.stderr):
            raise
        first_line = (exc.stderr or "").strip().partition("\n")[0]
        url = gh(*without_project(argv)).stdout.strip()
        sys.stderr.write(
            f'warning: not added to Project "{PROJECT_TITLE}": {first_line}\n'
        )
        return url


def cmd_open(args: argparse.Namespace) -> int:
    """Handles the ``open`` subcommand: files, touches or reopens a finding's issue.

    Syncs labels first since ``gh issue create --label`` fails on a label the repo lacks,
    then reads the body file, computes the fingerprint, and runs whatever ``plan_open``
    decides.

    Args:
        args: parsed CLI namespace for the ``open`` subcommand.

    Returns:
        The process exit code: 0 on success, 2 if the body file is missing, 3 if the
        fingerprint belongs to an issue closed as refuted.
    """
    if not args.body_file.is_file():
        sys.stderr.write(f"open: body file not found: {args.body_file}\n")
        return 2
    body = args.body_file.read_text()
    fp = fingerprint(args.title, args.file)
    labels = ["claude", f"severity/{args.severity}", f"kind/{args.kind}"]
    if args.domain:
        labels.append(f"domain/{args.domain}")
    if args.no_vetted_remediation:
        labels.append("no-vetted-remediation")
    # `gh issue create --label` fails on a label the repo does not have, so the first `open`
    # in a fresh repo has to create the label set before it can use it.
    run(plan_sync_labels(_existing_labels()), args.dry_run)
    existing = find_by_fingerprint(load_issues("all"), fp)
    outcome, code, plans = plan_open(
        existing,
        title=args.title,
        body=body,
        labels=labels,
        fp=fp,
        source=args.source,
        verify_by=args.verify_by,
    )
    if outcome == "created":
        if args.dry_run:
            run(plans, True)
            print(f"(dry-run) would create; fingerprint {fp}")
            return 0
        url = _create_with_optional_project(plans[0])
        print(f"#{url.rsplit('/', 1)[-1]} created  {url}")
        return 0
    # "created" is the only outcome plan_open returns for a missing issue, so every branch
    # below has one to name. Checked here rather than in each branch: the two of them read
    # `existing` four times between them.
    assert existing is not None
    if outcome == "refuted":
        print(
            f"#{existing['number']} refuted: closed on {(existing.get('closedAt') or '?')[:10]}; not reopened"
        )
        return code
    run(plans, args.dry_run)
    print(f"#{existing['number']} {outcome}  {existing.get('url', '')}")
    return 0


def plan_close(
    number: int,
    *,
    fixed: bool,
    pr: int | None,
    reason: str | None,
    comment: str | None = None,
) -> list[list[str]]:
    """Plans the gh argv to close an issue as fixed or refuted.

    Args:
        number: the issue number.
        fixed: True to close as completed, False to close as refuted.
        pr: the PR that fixed it, included in the close comment when given.
        reason: required when ``fixed`` is False; what disproved the finding.
        comment: overrides the default close comment (`verify` uses this to quote the
            verify-by command and its output instead of naming a PR).
    """
    n = str(number)
    if fixed:
        by = f" by PR #{pr}" if pr else ""
        text = comment or f"Fixed{by}."
        return [["issue", "close", n, "--reason", "completed", "--comment", text]]
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
    """Handles the ``touch`` subcommand: records a re-observation on an open issue.

    Args:
        args: parsed CLI namespace carrying ``number``, ``source`` and ``dry_run``.

    Returns:
        3 if the issue is already closed, 0 otherwise.
    """
    issue = _load_issue(args.number)
    if issue.get("state") == "CLOSED":
        why = "refuted" if "refuted" in label_names(issue) else "fixed"
        print(f"#{args.number} is closed ({why}); use open to re-file")
        return 3
    plans = plan_touch(issue, args.source)
    run(plans, args.dry_run)
    escalated = any(p[:2] == ["issue", "edit"] for p in plans)
    print(f"#{args.number} touched{' and escalated' if escalated else ''}")
    return 0


def cmd_close(args: argparse.Namespace) -> int:
    """Handles the ``close`` subcommand: closes an issue as fixed or refuted.

    Args:
        args: parsed CLI namespace for the ``close`` subcommand.

    Returns:
        2 if ``--refuted`` is combined with ``--pr`` or missing ``--reason``, 0 otherwise.
    """
    # argparse cannot express "--pr only with --fixed" across a mutually exclusive group.
    if args.refuted and args.pr:
        sys.stderr.write("close --pr goes with --fixed\n")
        return 2
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


DEFAULT_VERIFY_TIMEOUT = 120.0


def verify_close_comment(command: str, output: str, *, tail_lines: int = 20) -> str:
    """The close comment `verify --close` posts on a passing finding.

    Quotes the command and the tail of its combined stdout/stderr, so the record of why an
    issue closed lives on the issue rather than only in whoever ran `verify`.
    """
    lines = output.strip("\n").splitlines()
    tail = "\n".join(lines[-tail_lines:]) if lines else "(no output)"
    return (
        "Fixed: verify-by passed.\n\n"
        f"Command:\n```\n{command}\n```\n\n"
        f"Output (tail):\n```\n{tail}\n```\n"
    )


def run_verify_by(command: str, timeout: float) -> tuple[str, str]:
    """Runs a verify-by command and returns ``(verdict, detail)``.

    verdict is ``fixed`` (exit 0), ``still-open`` (nonzero exit) or ``error`` (refused by
    `classify_verify_command`, timed out, or could not be launched at all). detail is the
    command's combined stdout/stderr for ``fixed``/``still-open``, or the reason for
    ``error``.
    """
    reason = classify_verify_command(command)
    if not reason:
        return "error", "refused: not read-only by the repo's classifier"
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=str(REPO),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return "error", f"timed out after {timeout:g}s"
    except OSError as exc:
        return "error", str(exc)
    output = (proc.stdout or "") + (proc.stderr or "")
    return ("fixed" if proc.returncode == 0 else "still-open"), output


def verify_finding(issue: dict, timeout: float) -> tuple[str, str, str]:
    """Verifies one issue. Returns ``(verdict, detail, command)``.

    verdict adds ``no-verify-by`` to `run_verify_by`'s three, for an issue whose body
    carries no `## Verify-by` section at all — never run, so ``command`` is empty.
    """
    command = parse_verify_by(issue.get("body") or "")
    if not command:
        return "no-verify-by", "", ""
    verdict, detail = run_verify_by(command, timeout)
    return verdict, detail, command


def cmd_verify(args: argparse.Namespace) -> int:
    """Handles the ``verify`` subcommand: re-runs each finding's stored verify-by command.

    ``--dry-run`` only gates the gh writes a passing ``--close`` would make; the verify-by
    commands themselves always run — producing a verdict requires it, and they were already
    proven read-only by `classify_verify_command` before they run at all.

    Args:
        args: parsed CLI namespace carrying ``all``, ``numbers``, ``close``, ``timeout`` and
            ``dry_run``.

    Returns:
        2 if neither or both of ``--all``/issue numbers were given, 0 otherwise.
    """
    if args.all and args.numbers:
        sys.stderr.write("verify: pass --all or issue numbers, not both\n")
        return 2
    if not args.all and not args.numbers:
        sys.stderr.write("verify: need --all or at least one issue number\n")
        return 2
    issues = load_issues("open") if args.all else [_load_issue(n) for n in args.numbers]
    results = [
        (issue["number"], issue["title"], *verify_finding(issue, args.timeout))
        for issue in issues
    ]
    for number, title, verdict, _detail, _command in results:
        print(f"#{number:<5} {verdict:<11} {title}")
    if args.close:
        for number, _title, verdict, detail, command in results:
            if verdict != "fixed":
                continue
            comment = verify_close_comment(command, detail)
            run(
                plan_close(number, fixed=True, pr=None, reason=None, comment=comment),
                args.dry_run,
            )
            print(f"#{number} closed as fixed (verify-by)")
    return 0


def cmd_sync_labels(args: argparse.Namespace) -> int:
    plans = plan_sync_labels(_existing_labels())
    run(plans, args.dry_run)
    print(f"sync-labels: {len(plans)} label(s) created")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    """Handles the ``list`` subcommand: prints open findings as a table or JSON.

    Args:
        args: parsed CLI namespace carrying ``state`` and ``json``.
    """
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
                ("verify-by", r["verify_by"]),
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
    o.add_argument(
        "--verify-by",
        help="read-only command; exit 0 means fixed, non-zero means it still reproduces",
    )

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

    v = sub.add_parser(
        "verify",
        help="re-run each finding's verify-by command and report fixed/still-open",
    )
    _add_dry_run(v, suppress=True)
    v.add_argument("numbers", nargs="*", type=int, help="issue numbers to verify")
    v.add_argument("--all", action="store_true", help="verify every open finding")
    v.add_argument(
        "--close", action="store_true", help="close passing findings as fixed"
    )
    v.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_VERIFY_TIMEOUT,
        help="seconds before a verify-by command counts as an error",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    """Entry point: parses argv and dispatches to the matching subcommand handler.

    Catches gh failures at this outer layer so every subcommand handler can call ``gh``
    directly without duplicating error handling.

    Args:
        argv: command-line arguments, or None to use ``sys.argv``.

    Returns:
        The dispatched handler's exit code, or 1 if `gh` failed.
    """
    args = _parser().parse_args(argv)
    handler = {
        "sync-labels": cmd_sync_labels,
        "list": cmd_list,
        "open": cmd_open,
        "touch": cmd_touch,
        "close": cmd_close,
        "verify": cmd_verify,
    }[args.cmd]
    try:
        return handler(args)
    except (subprocess.SubprocessError, OSError) as exc:
        # OSError covers a missing `gh` binary; SubprocessError covers TimeoutExpired as
        # well as the CalledProcessError whose stderr is the message worth showing.
        if isinstance(exc, subprocess.CalledProcessError):
            sys.stderr.write(
                f"gh failed ({exc.returncode}): {(exc.stderr or '').strip()}\n"
            )
        else:
            sys.stderr.write(f"gh failed: {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
