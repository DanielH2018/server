#!/usr/bin/env python3
"""Generate docs/reference/backlog.md — the open findings Claude filed as GitHub Issues.

WHY THIS PAGE IS WORTH HAVING. The issues are the record; this page is the view that sits
beside the other generated references at docs.local, so "what is known-broken and unowned"
is answered where "what runs here" is, without opening GitHub.

WHAT IT READS. `scripts/dev/findings.py`'s row model over `gh issue list --label claude`.
The docs-refresh cron runs as the user whose gh is already authenticated to open the docs
PR, so this generator needs nothing it does not already have. A gh failure fails THIS
generator loudly; build_docs.py keeps rendering the others and exits non-zero, which is the
honest outcome — a page that quietly went stale would read as an empty backlog.

Usage::

    uv run python scripts/docs/gen_reference_backlog.py --out docs/reference/backlog.md
"""

from __future__ import annotations

import argparse
from pathlib import Path

# Reach the sibling package directories: a directly-invoked script gets only its own
# directory on sys.path, and pyproject's `pythonpath` is a pytest setting.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from dev.findings import issue_rows, load_issues, sort_key  # noqa: E402
from lib.docs_provenance import finish_generator, generated_banner, md_cell  # noqa: E402

SOURCE = "scripts/docs/gen_reference_backlog.py"


def render_markdown(rows: list[dict]) -> str:
    parts = [generated_banner(SOURCE), "# Backlog\n"]
    parts.append(
        "Findings Claude confirmed and did not fix in the session that found them, filed "
        "through `scripts/dev/findings.py` and labelled `claude` on GitHub. A row that has "
        "been re-observed three times carries **escalated** and needs a durable owner: a "
        "test, a hook or a CLAUDE.md rule. Close one from a PR body with `Closes #<n>`.\n"
    )
    if not rows:
        parts.append("No open findings.\n")
        return "\n".join(parts)
    parts.append(
        "| # | Severity | Kind | Domain | Finding | First seen | Re-observed |"
    )
    parts.append("|---|---|---|---|---|---|---|")
    for r in sorted(rows, key=sort_key):
        flags = []
        if r["escalated"]:
            flags.append("**escalated**")
        if r["no_vetted_remediation"]:
            flags.append("*no vetted remediation*")
        title = md_cell(r["title"]) + (" — " + ", ".join(flags) if flags else "")
        parts.append(
            f"| [#{r['number']}]({r['url']}) | {r['severity'] or '-'} | {r['kind'] or '-'} | "
            f"{r['domain'] or '-'} | {title} | {r['first_seen']} | {r['reobservations']} |"
        )
    return "\n".join(parts).rstrip("\n") + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--out", type=Path, required=True, help="output file path")
    args = parser.parse_args(argv)
    rows = issue_rows(load_issues("open"))
    return finish_generator(
        "gen_reference_backlog", args.out, rows, render_markdown, "finding"
    )


if __name__ == "__main__":
    raise SystemExit(main())
