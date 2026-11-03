"""The notice replay in ``fact_status.py report``: what the diff notice listed over the window.

``notice.moved_sections`` runs once per first-parent commit in the window, with the parent as
the base, so the replay always runs today's notice code over the whole window. Its counts are
the notice's yield, which G3 reads through ``moved`` and ``moved_prose_edited``.

``docs/adr/0023-fact-support-is-a-diff-notice.md`` records why the notice replaced the lock.
"""

from collections import Counter
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))  # scripts/
from lib.git import git

from .notice import Notice, moved_sections


def notice_replay(
    repo: Path,
    since: datetime,
    notice: Callable[[Path, str, str], list[Notice]] = moved_sections,
) -> dict:
    """Replay ``notice`` over every first-parent commit since ``since``, and count its rows.

    The window is the plain ``git log --since`` boundary. An exception from ``notice`` on one
    commit becomes a ``crashed`` entry and the replay goes on. ``content_notices`` names each
    commit and section of a row whose atom is not a path atom.
    """
    lines = git(
        "log",
        "--first-parent",
        f"--since={since.isoformat()}",
        "--format=%H %P",
        cwd=repo,
    ).stdout
    commits = 0
    kinds, edited = Counter(), Counter()
    crashed, content_notices = [], []
    with_notice = moved = moved_edited = 0
    for line in reversed(lines.splitlines()):
        sha, *parents = line.split()
        commits += 1
        if not parents:
            continue
        try:
            rows = notice(repo, parents[0], sha)
        except Exception as e:  # a crash is a finding, not a stop
            crashed.append({"commit": sha, "error": type(e).__name__})
            continue
        with_notice += bool(rows)
        for r in rows:
            kinds[r.kind] += 1
            edited[r.kind] += r.prose_edited
            if r.kind == "moved":
                moved += 1
                moved_edited += r.prose_edited
            if r.form != "path":
                content_notices.append({"commit": sha, "unit": r.unit})
    return {
        "commits": commits,
        "commits_with_notice": with_notice,
        "notices": dict(sorted(kinds.items())),
        "prose_edited": dict(sorted(edited.items())),
        "moved": moved,
        "moved_prose_edited": moved_edited,
        "crashed": crashed,
        "content_notices": content_notices,
    }


def _by_kind(counts: dict[str, int]) -> str:
    return ", ".join(f"{k} {n}" for k, n in counts.items()) or "none"


def render_replay(replay: dict) -> list[str]:
    lines = [
        "",
        "Notice replay over the window",
        f"  commits in the window       {replay['commits']}",
        f"  commits carrying a notice   {replay['commits_with_notice']}",
        f"  notices, by kind            {_by_kind(replay['notices'])}",
        f"  prose-edited, by kind       {_by_kind(replay['prose_edited'])}",
        f"  moved rows                  {replay['moved']}"
        f" ({replay['moved_prose_edited']} with the section's prose edited)",
        f"  crashes                     {len(replay['crashed'])}",
    ]
    lines += [f"    {c['commit'][:12]}  {c['error']}" for c in replay["crashed"]]
    lines.append(f"  content-form notices        {len(replay['content_notices'])}")
    return lines
