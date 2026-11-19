"""The comment-class replay in ``fact_status.py report``.

``comment_replay`` runs ``removed.stale_comment_uses``, the comment class of the removed-name
rule, once per first-parent commit in the window with the parent as the base, and lists every
row for a session to grade. G7 step 5 promotes the class to an error only if fewer than one
graded row in five is a false alarm (``docs/target-architecture/docs-guard/g7-removed-names.md``).
"""

from datetime import datetime
from pathlib import Path


from lib.git import git

from .notice import Tree
from .removed import Code, is_text, removed_lines, stale_comment_uses


def first_parent_commits(
    repo: Path, since: datetime, until: datetime
) -> list[tuple[str, str | None]]:
    """Each first-parent commit in the window, oldest first, with its first parent or None.

    The window is the plain ``git log --since``/``--until`` boundary.
    """
    lines = git(
        "log",
        "--first-parent",
        f"--since={since.isoformat()}",
        f"--until={until.isoformat()}",
        "--format=%H %P",
        cwd=repo,
    ).stdout
    commits = []
    for line in reversed(lines.splitlines()):
        sha, *parents = line.split()
        commits.append((sha, parents[0] if parents else None))
    return commits


def _advance(
    repo: Path, tree: Tree, at: str, to: str, tracked: set[str], texts: dict[str, str]
) -> None:
    """Move ``tracked`` and ``texts`` from the tree at ``at`` to the tree at ``to``."""
    raw = git("diff", "--raw", "-z", "--no-renames", at, to, cwd=repo).stdout
    fields = raw.split("\0")[:-1]
    for meta, rel in zip(fields[0::2], fields[1::2], strict=True):
        _old_mode, new_mode, _old, new, status = meta.lstrip(":").split()
        texts.pop(rel, None)
        if status == "D":
            tracked.discard(rel)
            continue
        tracked.add(rel)
        if new_mode != "160000" and not rel.endswith(".md"):
            text = tree.read_blob(new)
            if is_text(rel, text):
                texts[rel] = text


def comment_replay(repo: Path, since: datetime, until: datetime) -> dict:
    """Replay the comment class over every first-parent commit in the window, and list its rows.

    The tree is read once at the window's first base, then moved commit by commit, so each
    commit costs what it changed. Each row is a commit, the name it took out of code, and a
    comment at that commit's tree still naming it, as ``path:line``. An exception from the
    rule on one commit becomes a ``crashed`` entry and the replay goes on.
    """
    commits = first_parent_commits(repo, since, until)
    rows, crashed = [], []
    with_rows = 0
    bases = [parent for _, parent in commits if parent]
    if bases:
        with Tree(repo, bases[0]) as tree:
            tracked = set(tree.tracked)
            texts = {}
            for rel in sorted(tracked):
                text = None if rel.endswith(".md") else tree.text(rel)
                if text is not None and is_text(rel, text):
                    texts[rel] = text
            at = bases[0]
            for sha, parent in commits:
                _advance(repo, tree, at, sha, tracked, texts)
                at = sha
                if parent is None:
                    continue
                try:
                    found = stale_comment_uses(
                        Code(tracked, texts), removed_lines(repo, parent, sha)
                    )
                except Exception as e:  # a crash is a finding, not a stop
                    crashed.append({"commit": sha, "error": type(e).__name__})
                    continue
                with_rows += bool(found)
                rows += [
                    {"commit": sha, "token": tok, "place": place}
                    for tok, place in sorted(found)
                ]
    return {
        "commits": len(commits),
        "first": commits[0][0] if commits else None,
        "last": commits[-1][0] if commits else None,
        "commits_with_rows": with_rows,
        "crashed": crashed,
        "rows": rows,
    }


def render_comment_replay(replay: dict) -> list[str]:
    span = (
        f"{replay['first'][:12]}..{replay['last'][:12]}" if replay["first"] else "empty"
    )
    lines = [
        "",
        f"Comment-class replay over the window ({replay['commits']} commits, {span})",
        f"  {len(replay['rows'])} rows over {replay['commits_with_rows']} commits,"
        f" {len(replay['crashed'])} crashes; --json lists every row",
    ]
    lines += [f"    {c['commit'][:12]}  {c['error']}" for c in replay["crashed"]]
    return lines
