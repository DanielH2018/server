#!/usr/bin/env python3
"""Generate docs/reference/backlog.md — the findings Claude filed as GitHub Issues.

WHY THIS PAGE IS WORTH HAVING. The issues are the record; this page is the view that sits
beside the other generated references at docs.local, so "what is known-broken and unowned"
is answered where "what runs here" is, without opening GitHub.

TWO TABLES, ONE REGISTER. The first is the open backlog. The second is the settled register:
every finding closed `--accepted` (true, and the operator lives with it) or `--refuted` (a
skeptic disproved it), grouped by reviewer domain with the close reason beside it. The
reviewer agents under .claude/agents/ point at that table instead of carrying a hand-copied
"don't re-flag" list each -- three copies drifted apart before this page held the register
(#2169). `findings.py open` refuses to re-file either outcome; this table is where a reviewer
reads the same ruling before it spends a pass re-deriving it.

WHAT IT READS. `findings_lib/issue_model.py`'s row model over the `gh issue list --label
claude --state all` that `gh_calls.load_issues` runs; the open rows feed the backlog table
and the closed rows carrying an `accepted` or `refuted` label feed the settled one.
The docs-refresh cron runs as the user whose gh is already authenticated to open the docs
PR, so this generator needs nothing it does not already have. A gh failure fails THIS
generator loudly; build_docs.py keeps rendering the others and exits non-zero, which is the
honest outcome — a page that quietly went stale would read as an empty backlog.

Usage::

    uv run python scripts/docs/reference/backlog.py --out docs/reference/backlog.md
"""

import argparse
from pathlib import Path

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from dev.findings_lib.gh_calls import load_issues
from dev.findings_lib.issue_model import DOMAINS, issue_rows, sort_key
from lib.docs_provenance import finish_generator, generated_banner, md_cell

SOURCE = "scripts/docs/reference/backlog.py"


def render_markdown(rows: list[dict]) -> str:
    """Renders the backlog page: the banner, the open table, then the settled register.

    Args:
        rows: findings of any state in `issue_rows` shape, as returned by
            `dev.findings_lib.issue_model.issue_rows`.
    """
    parts = [generated_banner(SOURCE), "# Backlog\n"]
    open_rows = [r for r in rows if r["state"] == "OPEN"]
    settled = [
        r for r in rows if r["state"] != "OPEN" and (r["accepted"] or r["refuted"])
    ]
    parts.append(
        "Findings Claude confirmed and did not fix in the session that found them, filed "
        "through `scripts/dev/findings.py` and labelled `claude` on GitHub. A row that has "
        "been seen three times carries **escalated** (the filing plus two re-observations) "
        "and needs a durable owner: a "
        "test, a hook or a CLAUDE.md rule. Close one from a PR body with `Closes #<n>`. A "
        "row marked in the Verify-by column carries a description of how to check it in its "
        "issue body — run `findings.py verify --all` to print them. That command reports and "
        "runs nothing; closing stays with `findings.py close`.\n"
    )
    if not open_rows:
        parts.append("No open findings.\n")
    else:
        parts.extend(_open_table(open_rows))
    parts.append("")
    parts.extend(_settled_section(settled))
    return "\n".join(parts).rstrip("\n") + "\n"


def _open_table(rows: list[dict]) -> list[str]:
    parts = []
    parts.append(
        "| # | Severity | Kind | Domain | Finding | First seen | Re-observed | Claim | Verify-by |"
    )
    parts.append("|---|---|---|---|---|---|---|---|---|")
    for r in sorted(rows, key=sort_key):
        flags = []
        if r["escalated"]:
            flags.append("**escalated**")
        if r["no_vetted_remediation"]:
            flags.append("*no vetted remediation*")
        title = md_cell(r["title"]) + (" — " + ", ".join(flags) if flags else "")
        # Through `md_cell` like the title above it: a claim is a branch name, and a branch
        # name may carry a `|`, which silently adds a column and renders the table wrong.
        # The author check in `current_claim` is what keeps the VALUE trustworthy (#1280);
        # this keeps the row's shape intact whatever the value is.
        claimed = md_cell(r.get("claimed") or "-")
        verify_by = "✓" if r.get("verify_by") else "-"
        parts.append(
            f"| [#{r['number']}]({r['url']}) | {r['severity'] or '-'} | {r['kind'] or '-'} | "
            f"{r['domain'] or '-'} | {title} | {r['first_seen']} | {r['reobservations']} | "
            f"{claimed} | {verify_by} |"
        )
    return parts


def _settled_section(rows: list[dict]) -> list[str]:
    """The do-not-re-flag register: accepted and refuted closes, one table per domain."""
    parts = [
        "## Settled findings — do not re-flag\n",
        "Findings closed with `findings.py close --accepted` (true, and the operator chose to "
        "live with the trade-off) or `--refuted` (a skeptic disproved it), with the reason the "
        "close recorded. `findings.py open` refuses to re-file either. A reviewer agent reads "
        "its domain's table before it flags; a row it disagrees with is contradicted with new "
        "evidence at a cited symbol, never silently re-filed. A finding with no domain label "
        "sits under *unlabelled*.\n",
    ]
    if not rows:
        parts.append("No settled findings.\n")
        return parts
    by_domain: dict[str, list[dict]] = {}
    for r in rows:
        by_domain.setdefault(r["domain"] or "unlabelled", []).append(r)
    # DOMAINS order first so a reader finds a domain where the reviewer list has it, then
    # anything labelled outside that tuple, then the unlabelled rows last.
    order = [d for d in DOMAINS if d in by_domain]
    order += sorted(d for d in by_domain if d not in DOMAINS and d != "unlabelled")
    if "unlabelled" in by_domain:
        order.append("unlabelled")
    for domain in order:
        parts.append(f"### {domain}\n")
        parts.append("| # | Ruling | Finding | Reason |")
        parts.append("|---|---|---|---|")
        for r in sorted(by_domain[domain], key=lambda r: r["number"]):
            ruling = "accepted" if r["accepted"] else "refuted"
            parts.append(
                f"| [#{r['number']}]({r['url']}) | {ruling} | {md_cell(r['title'])} | "
                f"{md_cell(r.get('reason') or '-')} |"
            )
        parts.append("")
    return parts


def main(argv: list[str] | None = None) -> int:
    # The FIRST NON-BLANK line, not `[1]`. Line 1 of a module docstring is the blank line
    # after the summary, so `[1]` passed argparse an empty description and `--help` printed
    # none at all (#1272). `findings.py`'s `main` carries the same spelling.
    summary = next(line for line in __doc__.splitlines() if line.strip())
    parser = argparse.ArgumentParser(description=summary)
    parser.add_argument("--out", type=Path, required=True, help="output file path")
    args = parser.parse_args(argv)
    # `all`, not `open`: the settled table below the backlog reads the closed rows.
    rows = issue_rows(load_issues("all"))
    return finish_generator(
        "docs.reference.backlog", args.out, rows, render_markdown, "finding"
    )


if __name__ == "__main__":
    raise SystemExit(main())
