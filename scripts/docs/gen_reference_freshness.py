#!/usr/bin/env python3
"""Generate docs/reference/freshness.md -- every hand-written page, ranked by how far the
files it names have moved since it was last changed.

The per-page stamp `_mkdocs_freshness.py` appends answers "how old is this page". This
table answers "which page should someone reread next": pages with the most moved sources
first, then the oldest. `scripts/lib/doc_freshness.py` defines both numbers.

Usage::

    uv run python scripts/docs/gen_reference_freshness.py --out docs/reference/freshness.md
"""

from __future__ import annotations

import argparse
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from lib.doc_freshness import PageFreshness, survey  # noqa: E402
from lib.repo_paths import REPO  # noqa: E402


def ranked(pages: list[PageFreshness]) -> list[PageFreshness]:
    """Most moved sources first; among equals, the page changed longest ago."""
    return sorted(pages, key=lambda f: (-len(f.moved), f.changed, f.page))


def render_markdown(pages: list[PageFreshness]) -> str:
    from lib.docs_provenance import generated_banner

    parts = [generated_banner("scripts/docs/gen_reference_freshness.py")]
    parts.append("# Doc freshness\n")
    parts.append(
        f"{len(pages)} hand-written page(s). *Changed* is the page's last commit; *moved* "
        "counts the repo files the page names whose last commit is later than that. A moved "
        "source does not prove the page is wrong -- it marks the page to reread next. The "
        "generated reference pages are not listed: they are rebuilt from the tree.\n"
    )
    parts.append(
        "| Page | Changed | Sources named | Moved since | Most recently moved |"
    )
    parts.append("|---|---|---|---|---|")
    for f in ranked(pages):
        page = f.page.removeprefix("docs/")
        latest = max(f.moved, key=lambda pd: pd[1]) if f.moved else None
        latest_cell = f"`{latest[0]}` ({latest[1]})" if latest else "—"
        parts.append(
            f"| [{page}](../{page}) | {f.changed or '—'} | {len(f.sources)} | "
            f"{len(f.moved)} | {latest_cell} |"
        )
    return "\n".join(parts) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=_Path, required=True, help="output file path")
    args = parser.parse_args(argv)

    from lib.docs_provenance import finish_generator

    return finish_generator(
        "gen_reference_freshness", args.out, survey(REPO), render_markdown, "page"
    )


if __name__ == "__main__":
    raise SystemExit(main())
