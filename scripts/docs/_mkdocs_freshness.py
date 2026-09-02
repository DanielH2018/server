"""MkDocs hook: stamp every hand-written page with its age and its moved sources.

The generated reference pages already say when they were built. This gives the other
pages the equivalent, computed at build time from git so nothing is committed: the date of
the page's last change, how many repo files it names, and which of those changed after it.
`scripts/lib/doc_freshness.py` carries the definitions; `gen_reference_freshness.py` renders
the same numbers as one table across all pages, so a reader can go from "this page looks
old" to "these are the pages to reread".

Built at build time rather than committed for the same reason `build-info.json` is: a
stamp that changes whenever any source changes would be a commit on nearly every cron run.

The footer is appended as Markdown so the theme styles it like the rest of the page;
`assets/extra.css` mutes `.doc-freshness`.
"""

from __future__ import annotations

import posixpath
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.doc_freshness import (
    is_hand_written,
    last_change_dates,
    page_freshness,
    tracked_files,
)

TABLE = (
    "reference/freshness.md"  # src_uri of the page gen_reference_freshness.py writes
)

_state: dict[str, Any] = {}


def footer(fresh, table_link: str) -> str:
    """The stamp for one page, as Markdown appended below a rule.

    Args:
        fresh: the page's PageFreshness.
        table_link: the freshness table's URL relative to this page.
    """
    changed = fresh.changed or "not committed"
    parts = [f"Last content change: {changed}."]
    if fresh.sources:
        parts.append(f"Repo files this page names: {len(fresh.sources)}")
        if fresh.moved:
            listed = ", ".join(f"`{p}` ({d})" for p, d in fresh.moved)
            parts[-1] += f", of which {len(fresh.moved)} changed since: {listed}."
        else:
            parts[-1] += ", none changed since."
    parts.append(f"The [freshness table]({table_link}) ranks every page.")
    return '\n\n---\n\n<p class="doc-freshness" markdown>' + " ".join(parts) + "</p>\n"


def on_config(config):
    repo = Path(config.docs_dir).parent
    _state["repo"] = repo
    _state["tracked"] = tracked_files(repo)
    _state["dates"] = last_change_dates(repo)
    return config


def on_page_markdown(markdown, page, config, files):
    rel = f"docs/{page.file.src_uri}"
    if not is_hand_written(rel, markdown):
        return markdown
    fresh = page_freshness(rel, markdown, _state["dates"], _state["tracked"])
    link = posixpath.relpath(TABLE, posixpath.dirname(page.file.src_uri) or ".")
    return markdown.rstrip("\n") + footer(fresh, link)
